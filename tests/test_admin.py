"""超管后台 + 用户系统测试。

策略：用 FastAPI TestClient 启动 app；把 user_store / DATA_DIR 替换为临时目录，
避免污染真实 data/。每个 case 后清理临时目录。
"""

import pytest
from fastapi.testclient import TestClient


# ---------- 准备临时 data 目录 ----------
@pytest.fixture
def tmp_data_dir(monkeypatch, tmp_path):
    """把 UserStore 用的 DATA_DIR 替换到 tmp_path/data 并重新加载模块"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # 必须先 import app 包才会触发 users.py 顶层 user_store = UserStore()
    # 但 users.py 一加载就把 DATA_DIR 写死了。我们直接 patch DATA_DIR 和 users.USERS_FILE 等。
    from app import users as users_mod

    # patch 几个路径常量
    monkeypatch.setattr(users_mod, "DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(users_mod, "USERS_FILE", data_dir / "users.json", raising=False)
    monkeypatch.setattr(users_mod, "SESSIONS_FILE", data_dir / "sessions.json", raising=False)

    # 清空全局 store（如果之前已经创建过）
    users_mod.user_store.users.clear()
    users_mod.user_store.sessions.clear()
    users_mod.user_store._history.clear()
    # 现在触发种子
    users_mod.user_store._seed_if_empty()

    yield data_dir


# ---------- 准备 FastAPI TestClient ----------
@pytest.fixture
def client(tmp_data_dir):
    # main.py 在 import 时已经初始化了 user_store = UserStore()
    # 上面的 fixture 已经 patch 了路径和清了 store，但 user_store 实例本身的属性还指向旧文件
    # 我们直接重新创建 user_store 实例，并重新 patch 进 main 模块
    from app import main as main_mod
    from app import users as users_mod

    # 用新实例替换
    new_store = users_mod.UserStore()
    main_mod.user_store = new_store
    # 替换 admin 路由里直接引用的 user_store（其实就是 main_mod.user_store）

    return TestClient(main_mod.app)


# ---------- 辅助函数 ----------
def _admin_login(client: TestClient, username="www", password="1qaz!QAZ") -> str:
    r = client.post("/admin/login",
                    data={"username": username, "password": password,
                          "next": "/admin"},
                    follow_redirects=False)
    assert r.status_code == 303, f"登录失败: {r.status_code} {r.text[:200]}"
    return r.cookies.get("feiniu_admin", "")


# ============== 测试 ==============

def test_seed_creates_admin_and_default_user(tmp_data_dir):
    """首次部署会自动种子 www 超管 + Frank 普通用户"""
    from app import users as users_mod
    assert "www" in users_mod.user_store.users
    assert users_mod.user_store.is_admin("www")
    assert "Frank" in users_mod.user_store.users
    assert not users_mod.user_store.is_admin("Frank")
    # 数据落盘
    assert (tmp_data_dir / "users.json").exists()


def test_admin_login_page_redirects_when_authed(client):
    """已登录访问 /admin/login 会跳到 /admin"""
    _admin_login(client)
    r = client.get("/admin/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"


def test_admin_login_wrong_password(client):
    r = client.post("/admin/login",
                    data={"username": "www", "password": "wrong", "next": "/admin"},
                    follow_redirects=False)
    assert r.status_code == 401
    assert "账号或密码错误" in r.text


def test_non_admin_cannot_access_backend(client):
    """普通用户 Frank 登录后台会被拒绝"""
    r = client.post("/admin/login",
                    data={"username": "Frank", "password": "1qaz1QAZ",
                          "next": "/admin"},
                    follow_redirects=False)
    assert r.status_code == 403
    assert "不是超管" in r.text


def test_unauthed_admin_index_redirects(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


def test_admin_can_list_and_create_user(client):
    _admin_login(client)
    r = client.get("/admin")
    assert r.status_code == 200
    assert "www" in r.text
    assert "Frank" in r.text

    # 新增
    r = client.post("/admin/users/new",
                    data={"username": "alice", "password": "alice123"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"

    # 验证 alice 能用客户端登录
    r = client.post("/api/auth/login",
                    json={"username": "alice", "password": "alice123"})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "alice"
    assert body["is_admin"] is False


def test_duplicate_username_rejected(client):
    _admin_login(client)
    r = client.post("/admin/users/new",
                    data={"username": "Frank", "password": "x1234"},
                    follow_redirects=False)
    assert r.status_code == 400
    assert "已存在" in r.text


def test_short_password_rejected(client):
    _admin_login(client)
    r = client.post("/admin/users/new",
                    data={"username": "bob", "password": "abc"},
                    follow_redirects=False)
    assert r.status_code == 400


def test_admin_can_edit_user_password(client):
    _admin_login(client)
    # 给 Frank 重置密码
    r = client.post("/admin/users/Frank/edit",
                    data={"password": "newpass123", "is_admin": ""},
                    follow_redirects=False)
    assert r.status_code == 303

    # Frank 旧密码登不上
    r = client.post("/api/auth/login",
                    json={"username": "Frank", "password": "1qaz1QAZ"})
    assert r.status_code == 401
    # 新密码可以
    r = client.post("/api/auth/login",
                    json={"username": "Frank", "password": "newpass123"})
    assert r.status_code == 200


def test_admin_can_promote_and_demote(client):
    _admin_login(client)
    # 把 Frank 升为超管
    r = client.post("/admin/users/Frank/edit",
                    data={"password": "", "is_admin": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    # Frank 现在能用客户端 + 后台
    r = client.post("/api/auth/login",
                    json={"username": "Frank", "password": "1qaz1QAZ"})
    assert r.json()["is_admin"] is True
    # 再降为普通
    r = client.post("/admin/users/Frank/edit",
                    data={"password": "", "is_admin": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/api/auth/login",
                    json={"username": "Frank", "password": "1qaz1QAZ"})
    assert r.json()["is_admin"] is False


def test_admin_cannot_delete_self(client):
    _admin_login(client)
    r = client.post("/admin/users/www/delete",
                    data={"confirm": "yes"},
                    follow_redirects=False)
    assert r.status_code == 400
    assert "不能删除自己" in r.text
    # www 还在
    from app import main as main_mod
    assert "www" in main_mod.user_store.users


def test_admin_can_delete_user_cascades_history(client):
    from app import main as main_mod
    _admin_login(client)

    # 先给 Frank 上报点观看记录
    r = client.post("/api/auth/login",
                    json={"username": "Frank", "password": "1qaz1QAZ"})
    frank_token = r.json()["token"]
    # 找任意一个 video id
    videos = client.get("/api/videos").json()["videos"]
    if videos:
        vid = videos[0]["id"]
        r = client.post("/api/history",
                        headers={"Authorization": f"Bearer {frank_token}"},
                        json={"video_id": vid, "position": 10.0, "duration": 100.0})
        assert r.status_code == 200
        # 历史文件应存在
        # 直接读 store 内部
        assert len(main_mod.user_store._history.get("Frank", [])) > 0

    # 现在超管删 Frank
    r = client.post("/admin/users/Frank/delete",
                    data={"confirm": "yes"},
                    follow_redirects=False)
    assert r.status_code == 303
    # Frank 已不在
    assert "Frank" not in main_mod.user_store.users
    # 历史也被清
    assert "Frank" not in main_mod.user_store._history


def test_login_api_returns_is_admin(client):
    r = client.post("/api/auth/login",
                    json={"username": "www", "password": "1qaz!QAZ"})
    assert r.json()["is_admin"] is True
    r = client.post("/api/auth/login",
                    json={"username": "Frank", "password": "1qaz1QAZ"})
    assert r.json()["is_admin"] is False


def test_admin_api_blocks_non_admin_token(client):
    """普通用户用 token 调 /api/admin/* 应该 403"""
    r = client.post("/api/auth/login",
                    json={"username": "Frank", "password": "1qaz1QAZ"})
    frank_token = r.json()["token"]
    r = client.get("/api/admin/users",
                   headers={"Authorization": f"Bearer {frank_token}"})
    assert r.status_code == 403


def test_admin_api_allows_admin_token(client):
    r = client.post("/api/auth/login",
                    json={"username": "www", "password": "1qaz!QAZ"})
    admin_token = r.json()["token"]
    r = client.get("/api/admin/users",
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    names = [u["username"] for u in r.json()["users"]]
    assert "www" in names
    assert "Frank" in names


def test_existing_users_preserved_across_restart(tmp_data_dir):
    """重启后已有用户不会被清空"""
    from app import users as users_mod
    # 在第一个 store 里注册一个新用户
    users_mod.user_store.register("bob", "bob12345")
    assert "bob" in users_mod.user_store.users
    # 模拟重启：新建一个 UserStore 实例（会从 users.json 重新读）
    new_store = users_mod.UserStore()
    assert "bob" in new_store.users
    assert "www" in new_store.users
    assert "Frank" in new_store.users
    assert new_store.is_admin("www")
    assert not new_store.is_admin("Frank")


def test_logout_clears_cookie(client):
    _admin_login(client)
    # 退出
    r = client.post("/admin/logout", follow_redirects=False)
    assert r.status_code == 303
    # 再访问 /admin 应被踢回登录页
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


def test_register_api_requires_admin(client):
    """客户端公开注册入口已下线：未带 token 调用应 401，带普通用户 token 应 403"""
    # 完全无 token
    r = client.post("/api/auth/register",
                    json={"username": "mallory", "password": "x12345"})
    assert r.status_code == 401
    # 用普通用户 token
    r = client.post("/api/auth/login",
                    json={"username": "Frank", "password": "1qaz1QAZ"})
    frank_token = r.json()["token"]
    r = client.post("/api/auth/register",
                    headers={"Authorization": f"Bearer {frank_token}"},
                    json={"username": "mallory", "password": "x12345"})
    assert r.status_code == 403


def test_register_api_works_with_admin_token(client):
    """超管 token 调用 register 能正常建账号"""
    r = client.post("/api/auth/login",
                    json={"username": "www", "password": "1qaz!QAZ"})
    admin_token = r.json()["token"]
    r = client.post("/api/auth/register",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"username": "bob", "password": "bob12345"})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "bob"
    assert body["is_admin"] is False
    # 用新账号登录
    r = client.post("/api/auth/login",
                    json={"username": "bob", "password": "bob12345"})
    assert r.status_code == 200
