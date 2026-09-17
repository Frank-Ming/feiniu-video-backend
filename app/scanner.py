"""视频扫描与缓存

设计要点（针对 30k+ 大目录优化）：
1. 首次访问 /api/videos 时立即启动后台扫描（不阻塞响应）
2. 扫描结果缓存到磁盘 (cache/videos.json)，下次启动秒开
3. 时长探测完全异步：列表接口只返回 path/size/name/dir；时长在前端点开视频时按需探测
4. 用户主动访问 /api/videos?refresh=1 才强制重扫
5. 通过 mtime 比较感知目录变更，避免重复扫
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import CONFIG


logger = logging.getLogger(__name__)


@dataclass
class VideoItem:
    id: str
    name: str
    path: str
    full_path: str
    size: int
    mtime: float
    dir: str = ""
    duration: Optional[float] = None    # 懒探测，可能为 None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------
_FFPROBE_BIN: Optional[str] = None


def _get_ffprobe() -> Optional[str]:
    global _FFPROBE_BIN
    if _FFPROBE_BIN is not None:
        return _FFPROBE_BIN
    try:
        import imageio_ffmpeg
        ffmpeg = (imageio_ffmpeg.get_ffmpeg_exe()
                  if hasattr(imageio_ffmpeg, "get_ffmpeg_exe")
                  else imageio_ffmpeg.get_ffmpeg())
        ffprobe = Path(ffmpeg).with_name(
            "ffprobe" + (ffmpeg[-4:] if ffmpeg.lower().endswith(".exe") else "")
        )
        _FFPROBE_BIN = str(ffprobe) if ffprobe.exists() else ffmpeg
    except Exception:
        _FFPROBE_BIN = None
    return _FFPROBE_BIN


def probe_duration(path: Path) -> Optional[float]:
    """探测视频时长（秒）。失败返回 None。"""
    bin_path = _get_ffprobe()
    if bin_path is None:
        return None
    try:
        is_ffmpeg = bin_path.endswith(("ffmpeg", "ffmpeg.exe"))
        if is_ffmpeg:
            proc = subprocess.run(
                [bin_path, "-i", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            import re
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr or "")
            if m:
                h, mi, s = m.groups()
                return int(h) * 3600 + int(mi) * 60 + float(s)
            return None
        proc = subprocess.run(
            [bin_path, "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
        dur = (data.get("format") or {}).get("duration")
        return float(dur) if dur else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
class VideoScanner:
    def __init__(self,
                 root: Optional[str] = None,
                 extensions: Optional[List[str]] = None,
                 recursive: Optional[bool] = None,
                 cache_ttl: Optional[int] = None):
        self.root = Path(root or CONFIG.video.root).expanduser().resolve()
        self.extensions = tuple(e.lower() for e in (extensions or CONFIG.video.extensions))
        self.recursive = bool(CONFIG.video.recursive if recursive is None else recursive)
        self.cache_ttl = int(cache_ttl if cache_ttl is not None else CONFIG.video.cache_ttl)

        # 缓存目录
        self.cache_dir = Path(__file__).resolve().parent.parent / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "videos.json"
        self.state_file = self.cache_dir / "scan_state.json"

        self._cache: List[VideoItem] = []
        self._cache_time: float = 0.0
        self._scan_lock = asyncio.Lock()
        self._scan_task: Optional[asyncio.Task] = None
        self._scanning: bool = False

    @staticmethod
    def _make_id(path: Path) -> str:
        return hashlib.md5(str(path).encode("utf-8")).hexdigest()[:16]

    # ---- 持久化缓存 ----
    def _load_disk_cache(self) -> List[VideoItem]:
        if not self.cache_file.exists():
            return []
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return [VideoItem(**item) for item in raw]
        except Exception as e:
            logger.warning("读取缓存失败: %s", e)
            return []

    def _save_disk_cache(self, items: List[VideoItem]) -> None:
        try:
            self.cache_file.write_text(
                json.dumps([asdict(i) for i in items], ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("写缓存失败: %s", e)

    def _root_signature(self) -> Optional[dict]:
        """对根目录算一个轻量签名（mtime + 文件计数 + 子目录 mtime 之和）"""
        try:
            st = self.root.stat()
            file_count = 0
            total = 0
            for p in self.root.rglob("*"):
                try:
                    s = p.stat()
                except OSError:
                    continue
                if p.is_dir():
                    total += int(s.st_mtime)
                else:
                    file_count += 1
            return {
                "root_mtime": int(st.st_mtime),
                "file_count": file_count,
                "dir_mtime_sum": total,
            }
        except Exception:
            return None

    def _load_signature(self) -> Optional[dict]:
        if not self.state_file.exists():
            return None
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_signature(self, sig: dict) -> None:
        try:
            self.state_file.write_text(json.dumps(sig), encoding="utf-8")
        except Exception:
            pass

    # ---- 主入口 ----
    async def warm_up(self) -> None:
        """服务启动时调用一次：尝试用磁盘缓存做秒开"""
        cached = self._load_disk_cache()
        if cached:
            self._cache = cached
            self._cache_time = time.time()
            logger.info("从磁盘缓存加载 %d 个视频", len(cached))

        # 然后异步做一次校验，看是否需要全量重扫
        sig_now = self._root_signature()
        sig_prev = self._load_signature()
        if sig_now and sig_prev == sig_now:
            logger.info("目录无变更，跳过扫描")
            return
        await self.schedule_scan(force=True)

    async def schedule_scan(self, force: bool = False) -> None:
        """后台异步扫描：不阻塞调用方"""
        if self._scan_task and not self._scan_task.done():
            return
        self._scan_task = asyncio.create_task(self._scan_async(force=force))

    @property
    def is_scanning(self) -> bool:
        return self._scanning

    async def _scan_async(self, force: bool = False) -> None:
        async with self._scan_lock:
            self._scanning = True
            try:
                logger.info("开始扫描目录: %s", self.root)
                t0 = time.time()
                items = await asyncio.to_thread(self._scan_sync)
                self._cache = items
                self._cache_time = time.time()
                sig = self._root_signature()
                if sig:
                    self._save_signature(sig)
                self._save_disk_cache(items)
                logger.info("扫描完成: %d 个视频, 耗时 %.1fs",
                            len(items), time.time() - t0)
            finally:
                self._scanning = False

    def _scan_sync(self) -> List[VideoItem]:
        if not self.root.exists():
            return []
        files: List[Path] = []
        if self.recursive:
            for p in self.root.rglob("*"):
                if p.is_file() and p.suffix.lower() in self.extensions:
                    files.append(p)
        else:
            for p in self.root.iterdir():
                if p.is_file() and p.suffix.lower() in self.extensions:
                    files.append(p)

        items: List[VideoItem] = []
        for fp in files:
            try:
                st = fp.stat()
            except OSError:
                continue
            try:
                rel = fp.relative_to(self.root).as_posix()
            except ValueError:
                rel = fp.name
            parts = rel.split("/")
            first_dir = parts[0] if len(parts) > 1 else ""
            items.append(VideoItem(
                id=self._make_id(fp),
                name=fp.stem,
                path=rel,
                full_path=str(fp),
                size=st.st_size,
                mtime=st.st_mtime,
                dir=first_dir,
                duration=None,   # 懒探测
            ))
        items.sort(key=lambda x: x.mtime, reverse=True)
        return items

    # ---- 对外 API ----
    def list_videos(self) -> List[VideoItem]:
        """同步获取列表（不触发扫描，只取缓存）"""
        if not self._cache:
            disk = self._load_disk_cache()
            if disk:
                self._cache = disk
                self._cache_time = time.time()
        return self._cache

    async def ensure_scanned(self) -> None:
        """确保至少触发过一次扫描；若尚未完成则等待它结束"""
        if self._scan_task and not self._scan_task.done():
            await self._scan_task
        elif not self._cache:
            # 兜底同步扫一次（仅用于冷启动空缓存）
            await self._scan_async()

    async def probe_duration_async(self, video_id: str) -> Optional[float]:
        """按 id 探测时长（写入缓存，不阻塞）"""
        for v in self._cache:
            if v.id == video_id:
                if v.duration is not None:
                    return v.duration
                d = await asyncio.to_thread(probe_duration, Path(v.full_path))
                if d is not None:
                    v.duration = d
                    self._save_disk_cache(self._cache)
                return d
        return None

    def get_by_id(self, video_id: str) -> Optional[VideoItem]:
        for v in self.list_videos():
            if v.id == video_id:
                return v
        return None

    def list_dirs(self) -> List[Dict]:
        counter: Dict[str, int] = {}
        for v in self.list_videos():
            d = v.dir or "(根目录)"
            counter[d] = counter.get(d, 0) + 1
        return [{"name": k, "count": v} for k, v in
                sorted(counter.items(), key=lambda x: -x[1])]


# 全局单例
scanner = VideoScanner()


def filter_items(items: List[VideoItem],
                 dirs: Optional[List[str]] = None,
                 max_seconds: Optional[float] = None,
                 min_seconds: Optional[float] = None,
                 require_duration: bool = False) -> List[VideoItem]:
    """按子文件夹 + 时长筛选"""
    result = items
    if dirs:
        target = set(dirs)
        result = [v for v in result if (v.dir or "(根目录)") in target]
    if min_seconds is not None:
        if require_duration:
            result = [v for v in result if v.duration is not None and v.duration >= min_seconds]
        else:
            # 没探测到时长的也保留（避免误过滤）
            result = [v for v in result if v.duration is None or v.duration >= min_seconds]
    if max_seconds is not None:
        if require_duration:
            result = [v for v in result if v.duration is not None and v.duration <= max_seconds]
        else:
            result = [v for v in result if v.duration is None or v.duration <= max_seconds]
    return result
