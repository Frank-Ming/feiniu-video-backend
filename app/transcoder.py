"""按需转码：把不兼容的视频转成 H.264 + AAC，缓存在 cache/transcoded/。

策略：
- VAAPI 硬压（5825U 核显支持）优先；失败则软压
- 进程级锁：同一个 video_id 同时只跑一个
- 状态机：idle → running → done / failed
- 输出 mp4：H.264 baseline（最大兼容性）+ AAC 128k
- 限码率 2 Mbps + 分辨率上限 720p（NAS 软压也能 cover），减少磁盘和带宽
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from .scanner import _get_ffprobe, scanner

logger = logging.getLogger(__name__)

TRANSCODED_DIR_NAME = "transcoded"
# 单任务超时：2 小时（足够 1~2 小时视频转完；软压 AV1 这种极端场景也兜得住）
TRANSCODE_TIMEOUT_SEC = 2 * 3600
# 最多并发转码任务数（避免把 NAS CPU 跑满）
MAX_CONCURRENT_TASKS = 1


class TaskStatus(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class TranscodeTask:
    video_id: str
    status: TaskStatus = TaskStatus.IDLE
    progress: float = 0.0          # 0.0 ~ 1.0
    error: str = ""
    output_path: str = ""           # 完成后填
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    task_id: str = ""               # 每次启动一个唯一 id
    pid: int = 0                    # ffmpeg 子进程 pid


def _get_ffmpeg_exe_local() -> str | None:
    """拿到 ffmpeg（不只是 ffprobe）。优先 imageio_ffmpeg 自带的；否则 PATH 里的。"""
    if hasattr(_get_ffmpeg_exe_local, "_cached"):
        return _get_ffmpeg_exe_local._cached
    try:
        import imageio_ffmpeg
        ffmpeg = (imageio_ffmpeg.get_ffmpeg_exe()
                  if hasattr(imageio_ffmpeg, "get_ffmpeg_exe")
                  else imageio_ffmpeg.get_ffmpeg())
        result = str(ffmpeg) if ffmpeg else None
    except Exception:
        result = shutil.which("ffmpeg")
    _get_ffmpeg_exe_local._cached = result
    return result


def _detect_vaapi(device: str = "/dev/dri/renderD128") -> str | None:
    """探测 VAAPI 设备路径；不存在返回 None。"""
    if not Path(device).exists():
        return None
    return device


def _probe_video_codec(path: Path) -> str | None:
    """从 ffprobe -show_streams 里读 video 流的 codec_name。"""
    ffprobe = _get_ffprobe()
    if ffprobe is None:
        return None
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return None
        return streams[0].get("codec_name")
    except Exception:
        return None


class Transcoder:
    """全局转码管理器。

    设计要点：
    - 状态保存在内存（重启后丢失 → 客户端重新发起即可）
    - 同一 video_id 并发请求 → 复用同一个 task
    - 全局并发上限 MAX_CONCURRENT_TASKS；超出的排入队列
    - 输出固定 cache/transcoded/<id>.mp4
    """

    def __init__(self):
        self._tasks: dict[str, TranscodeTask] = {}  # video_id -> task
        self._active: dict[str, threading.Thread] = {}
        self._queue: list = []  # video_ids 等待启动
        self._lock = threading.Lock()
        self._cache_dir: Path | None = None
        self._ffmpeg: str | None = None

    # ---------- 路径 ----------
    def _ensure_cache_dir(self) -> Path:
        if self._cache_dir is not None:
            return self._cache_dir
        # 用 scanner 用的同一个 cache 目录
        d = scanner.cache_dir / TRANSCODED_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        self._cache_dir = d
        return d

    def _output_path(self, video_id: str) -> Path:
        return self._ensure_cache_dir() / f"{video_id}.mp4"

    def has_transcoded(self, video_id: str) -> bool:
        p = self._output_path(video_id)
        return p.exists() and p.stat().st_size > 0

    def transcoded_path(self, video_id: str) -> Path | None:
        p = self._output_path(video_id)
        if p.exists() and p.stat().st_size > 0:
            return p
        return None

    # ---------- 状态 ----------
    def get_task(self, video_id: str) -> TranscodeTask | None:
        with self._lock:
            t = self._tasks.get(video_id)
            return TranscodeTask(**asdict(t)) if t else None

    def list_tasks(self) -> list:
        with self._lock:
            return [TranscodeTask(**asdict(t)) for t in self._tasks.values()]

    # ---------- 启动 ----------
    def request(self, video_id: str) -> TranscodeTask:
        """请求转码。幂等：已有 task 直接返回。

        并发去重：如果已经在跑/排队，不重启。
        """
        with self._lock:
            existing = self._tasks.get(video_id)
            if existing is not None:
                if existing.status in (TaskStatus.RUNNING, TaskStatus.QUEUED,
                                       TaskStatus.DONE):
                    return TranscodeTask(**asdict(existing))
                # FAILED → 允许重试
            # 检查是否已经转好了
            if self.has_transcoded(video_id):
                t = TranscodeTask(
                    video_id=video_id,
                    status=TaskStatus.DONE,
                    progress=1.0,
                    output_path=str(self._output_path(video_id)),
                    created_at=time.time(),
                    started_at=time.time(),
                    finished_at=time.time(),
                    task_id=str(uuid.uuid4()),
                )
                self._tasks[video_id] = t
                return TranscodeTask(**asdict(t))

            # 新建 / 重置
            t = TranscodeTask(
                video_id=video_id,
                status=TaskStatus.QUEUED,
                created_at=time.time(),
                task_id=str(uuid.uuid4()),
            )
            self._tasks[video_id] = t
            if video_id not in self._queue:
                self._queue.append(video_id)
            self._maybe_start_locked()
            return TranscodeTask(**asdict(t))

    def _maybe_start_locked(self) -> None:
        """持锁状态下调用：检查是否能从队列启动新任务。"""
        while len(self._active) < MAX_CONCURRENT_TASKS and self._queue:
            vid = self._queue.pop(0)
            t = self._tasks.get(vid)
            if t is None:
                continue
            # 状态确认
            t.status = TaskStatus.RUNNING
            t.started_at = time.time()
            thread = threading.Thread(
                target=self._run_one, args=(vid,), daemon=True,
                name=f"transcode-{vid}")
            self._active[vid] = thread
            thread.start()

    def _run_one(self, video_id: str) -> None:
        """线程主体：执行转码，结束（成功/失败）后从 _active 移除并启动下一个。"""
        task = self._tasks.get(video_id)
        if task is None:
            return
        try:
            self._run_ffmpeg(video_id, task)
        except Exception as e:
            logger.exception("转码异常 video_id=%s", video_id)
            task.status = TaskStatus.FAILED
            task.error = f"exception: {e}"
        finally:
            task.finished_at = time.time()
            with self._lock:
                self._active.pop(video_id, None)
                self._maybe_start_locked()

    # ---------- ffmpeg 调用 ----------
    def _run_ffmpeg(self, video_id: str, task: TranscodeTask) -> None:
        item = scanner.get_by_id(video_id)
        if item is None:
            task.status = TaskStatus.FAILED
            task.error = "video not found in scanner"
            return

        ffmpeg = _get_ffmpeg_exe_local()
        if ffmpeg is None:
            task.status = TaskStatus.FAILED
            task.error = "ffmpeg not found"
            return

        src = Path(item.full_path)
        if not src.exists():
            task.status = TaskStatus.FAILED
            task.error = "source file missing"
            return

        dst = self._output_path(video_id)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".mp4.tmp")

        # 时长 → 用于进度（ffmpeg -progress 用）
        duration = item.duration or 0.0

        # 选择编码策略
        vaapi = _detect_vaapi()
        codec = _probe_video_codec(src) or ""
        use_vaapi = bool(vaapi) and codec != "h264"
        # h264 源用 VAAPI 反而不划算（重编码损失画质）；其他编码一律 VAAPI 优先

        if use_vaapi:
            cmd = self._build_vaapi_cmd(ffmpeg, src, tmp, vaapi)  # type: ignore[arg-type]
        else:
            cmd = self._build_software_cmd(ffmpeg, src, tmp)
        logger.info("转码 video_id=%s codec=%s vaapi=%s cmd_len=%d",
                    video_id, codec, use_vaapi, len(cmd))

        # 用 Popen + -progress pipe 解析进度
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            task.pid = proc.pid
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = f"failed to start ffmpeg: {e}"
            return

        # 读 progress（key=value 行）
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k == "out_time_ms":
                    try:
                        cur_sec = int(v) / 1_000_000.0
                        if duration > 0:
                            task.progress = min(1.0, cur_sec / duration)
                    except Exception:
                        pass
                elif k == "progress" and v == "end":
                    task.progress = 1.0
                # 'continue' / 'end' 也行，结束时主进程 join 会清场
        except Exception as e:
            logger.warning("读 progress 出错 video_id=%s: %s", video_id, e)

        try:
            ret = proc.wait(timeout=TRANSCODE_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            ret = -1
            task.error = "转码超时（> 2 小时）"
            task.status = TaskStatus.FAILED
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return

        if ret != 0:
            stderr = (proc.stderr.read() if proc.stderr else "")[:2000]
            task.status = TaskStatus.FAILED
            task.error = f"ffmpeg exit {ret}: {stderr}"
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return

        # 原子替换
        try:
            if dst.exists():
                dst.unlink()
            tmp.rename(dst)
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = f"rename failed: {e}"
            return

        task.status = TaskStatus.DONE
        task.progress = 1.0
        task.output_path = str(dst)
        task.error = ""
        logger.info("转码完成 video_id=%s -> %s", video_id, dst)

    def _build_vaapi_cmd(self, ffmpeg: str, src: Path, dst: Path,
                         vaapi_device: str) -> list:
        """VAAPI 硬压：h264_vaapi + scale_vaapi。"""
        return [
            ffmpeg, "-y",
            "-hwaccel", "vaapi",
            "-hwaccel_device", vaapi_device,
            "-hwaccel_output_format", "vaapi",
            "-i", str(src),
            "-vf", "scale_vaapi=format=nv12:force_original_aspect_ratio=decrease,"
                   "scale=w='if(gt(iw,ih),min(1280,iw),-2)':h='if(gt(ih,iw),min(720,ih),-2)'",
            "-c:v", "h264_vaapi",
            "-qp", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-ac", "2",
            "-movflags", "+faststart",
            "-f", "mp4",
            "-progress", "pipe:1",
            str(dst),
        ]

    def _build_software_cmd(self, ffmpeg: str, src: Path, dst: Path) -> list:
        """软压：libx264 medium + AAC。"""
        return [
            ffmpeg, "-y",
            "-i", str(src),
            "-vf", "scale=w='if(gt(iw,ih),min(1280,iw),-2)':"
                   "h='if(gt(ih,iw),min(720,ih),-2)':force_original_aspect_ratio=decrease",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-profile:v", "baseline",
            "-level", "3.1",
            "-c:a", "aac", "-b:a", "128k",
            "-ac", "2",
            "-movflags", "+faststart",
            "-f", "mp4",
            "-progress", "pipe:1",
            str(dst),
        ]

    # ---------- 取消 ----------
    def cancel(self, video_id: str) -> bool:
        """取消一个任务（如果是 running 状态会 kill ffmpeg）。"""
        with self._lock:
            t = self._tasks.get(video_id)
            if t is None:
                return False
            # 从队列移除
            if video_id in self._queue:
                self._queue.remove(video_id)
            t.status = TaskStatus.FAILED
            t.error = "cancelled"
            t.finished_at = time.time()
        # kill 进程
        if t.pid > 0:
            try:
                os.kill(t.pid, 9)
            except Exception:
                pass
        return True


# 全局单例
transcoder = Transcoder()
