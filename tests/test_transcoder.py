"""转码器测试：用 monkeypatch 模拟 ffmpeg 调用。
不依赖真实 ffmpeg 二进制（避免测试环境缺失）。
"""
import time

import pytest

from app import scanner as scanner_mod
from app import transcoder as tc_mod
from app.transcoder import TaskStatus, Transcoder


# ---------- fixtures ----------
@pytest.fixture
def trans(monkeypatch, tmp_path):
    """给一个隔离的 transcoder 实例 + mock ffmpeg 路径"""
    monkeypatch.setattr(tc_mod, "_get_ffmpeg_exe_local",
                        lambda: str(tmp_path / "fake-ffmpeg"))
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    t = Transcoder()
    t._cache_dir = cache_dir / "transcoded"
    t._cache_dir.mkdir(parents=True, exist_ok=True)
    return t


@pytest.fixture
def fresh_app(monkeypatch, tmp_path):
    """重新初始化 user_store 到 tmp_path，避免污染全局"""
    from app import main as main_mod
    from app import users as users_mod
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(users_mod, "DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(users_mod, "USERS_FILE", data_dir / "users.json",
                        raising=False)
    monkeypatch.setattr(users_mod, "SESSIONS_FILE",
                        data_dir / "sessions.json", raising=False)
    users_mod.user_store.users.clear()
    users_mod.user_store.sessions.clear()
    users_mod.user_store._history.clear()
    users_mod.user_store._seed_if_empty()
    new_store = users_mod.UserStore()
    main_mod.user_store = new_store
    return main_mod.app


def _login_token(fresh_app, username: str = "Frank",
                password: str = "1qaz1QAZ") -> str:
    from fastapi.testclient import TestClient
    c = TestClient(fresh_app)
    r = c.post("/api/auth/login",
               json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ============== Transcoder 单元测试 ==============

def test_has_transcoded_false_when_no_file(trans):
    assert trans.has_transcoded("nope") is False


def test_request_creates_queued_or_done(trans, monkeypatch):
    # 没有源文件：scanner.get_by_id 返回 None → task 标记 FAILED
    monkeypatch.setattr(scanner_mod.scanner, "get_by_id", lambda vid: None)
    t = trans.request("vid-X")
    assert t.status == TaskStatus.FAILED
    assert "not found" in t.error


def test_request_dedup_returns_same_task(trans, monkeypatch):
    """重复请求同一个 video_id → 同一个 task，不重启"""
    fake_item = type("I", (), {
        "id": "v1", "full_path": "/tmp/src.mp4", "duration": 100.0,
    })()
    monkeypatch.setattr(scanner_mod.scanner, "get_by_id", lambda vid: fake_item)
    t1 = trans.request("v1")
    t2 = trans.request("v1")
    assert t1.task_id == t2.task_id


def test_request_failed_can_retry(trans, monkeypatch):
    """FAILED 状态允许重试（task_id 会换）"""
    fake_item = type("I", (), {
        "id": "v1", "full_path": "/tmp/src.mp4", "duration": 100.0,
    })()
    monkeypatch.setattr(scanner_mod.scanner, "get_by_id", lambda vid: fake_item)
    t1 = trans.request("v1")
    with trans._lock:
        trans._tasks["v1"].status = TaskStatus.FAILED
        trans._tasks["v1"].error = "prev fail"
    t2 = trans.request("v1")
    assert t2.task_id != t1.task_id
    assert t2.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)


def test_has_transcoded_returns_true_after_file_present(trans):
    out = trans._output_path("abc")
    out.write_bytes(b"x" * 10)
    assert trans.has_transcoded("abc") is True
    assert trans.transcoded_path("abc") == out


def test_request_returns_done_if_cache_exists(trans, monkeypatch):
    """已经转好的视频，request 直接返 DONE 不重启"""
    out = trans._output_path("cached")
    out.write_bytes(b"x" * 10)
    t = trans.request("cached")
    assert t.status == TaskStatus.DONE
    assert t.progress == 1.0


def test_list_tasks_returns_copy(trans):
    """list_tasks 返回拷贝，外部修改不影响内部"""
    out = trans._output_path("a")
    out.write_bytes(b"x" * 10)
    trans.request("a")
    tasks = trans.list_tasks()
    assert len(tasks) == 1
    tasks[0].status = "hacked"
    # 内部状态不变
    fresh = trans.get_task("a")
    assert fresh.status == TaskStatus.DONE


def test_cancel_nonexistent_returns_false(trans):
    assert trans.cancel("nope") is False


def test_actual_ffmpeg_run_succeeds(monkeypatch, tmp_path):
    """用 fake ffmpeg（python 脚本）跑一次完整流程：mock Popen 不可行，
    改成 mock 整个 _run_ffmpeg 方法，验证 status 转换正确。"""
    trans = Transcoder()
    trans._cache_dir = tmp_path / "cache" / "transcoded"
    trans._cache_dir.mkdir(parents=True, exist_ok=True)

    # mock _run_ffmpeg：直接生成 DONE task
    def fake_run(self, video_id, task):
        out = trans._output_path(video_id)
        out.write_bytes(b"x" * 100)
        task.status = TaskStatus.DONE
        task.progress = 1.0
        task.output_path = str(out)
    monkeypatch.setattr(Transcoder, "_run_ffmpeg", fake_run)

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    fake_item = type("I", (), {
        "id": "v1", "full_path": str(src), "duration": 10.0,
    })()
    monkeypatch.setattr(scanner_mod.scanner, "get_by_id", lambda vid: fake_item)
    monkeypatch.setattr(tc_mod, "_get_ffmpeg_exe_local", lambda: "/bin/true")

    t = trans.request("v1")
    # 等线程结束
    time.sleep(0.5)
    t2 = trans.get_task("v1")
    assert t2.status == TaskStatus.DONE
    assert trans.has_transcoded("v1")


def test_actual_ffmpeg_run_fails(monkeypatch, tmp_path):
    """_run_ffmpeg 抛异常 → task 标记 FAILED"""
    trans = Transcoder()
    trans._cache_dir = tmp_path / "cache" / "transcoded"
    trans._cache_dir.mkdir(parents=True, exist_ok=True)

    def fake_run(self, video_id, task):
        raise RuntimeError("simulated ffmpeg crash")
    monkeypatch.setattr(Transcoder, "_run_ffmpeg", fake_run)

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    fake_item = type("I", (), {
        "id": "v2", "full_path": str(src), "duration": 10.0,
    })()
    monkeypatch.setattr(scanner_mod.scanner, "get_by_id", lambda vid: fake_item)
    monkeypatch.setattr(tc_mod, "_get_ffmpeg_exe_local", lambda: "/bin/true")

    trans.request("v2")
    time.sleep(0.5)
    t = trans.get_task("v2")
    assert t.status == TaskStatus.FAILED
    assert "exception" in t.error


# ============== HTTP API 测试 ==============

def test_transcode_api_requires_login(fresh_app):
    from fastapi.testclient import TestClient
    c = TestClient(fresh_app)
    r = c.post("/api/transcode/anything")
    assert r.status_code == 401


def test_transcode_api_404_for_unknown_video(fresh_app):
    from fastapi.testclient import TestClient
    c = TestClient(fresh_app)
    tok = _login_token(fresh_app)
    r = c.post("/api/transcode/unknown-video-id-xyz",
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 404


def test_transcode_status_for_never_requested(fresh_app):
    from fastapi.testclient import TestClient
    c = TestClient(fresh_app)
    tok = _login_token(fresh_app)
    r = c.get("/api/transcode/any-id",
             headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    body = r.json()
    assert body["has_cache"] is False
    assert body["task"] is None


def test_transcode_cancel_for_unknown(fresh_app):
    """取消不存在的任务 → ok=False 但不报错"""
    from fastapi.testclient import TestClient
    c = TestClient(fresh_app)
    tok = _login_token(fresh_app)
    r = c.post("/api/transcode/no-such/cancel",
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
