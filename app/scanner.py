"""视频扫描与缓存"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import CONFIG


@dataclass
class VideoItem:
    """单条视频信息"""
    id: str              # 基于路径的稳定 id
    name: str            # 文件名（不含扩展名）
    path: str            # 相对于 root 的路径
    full_path: str       # 绝对路径
    size: int            # 字节
    mtime: float         # 修改时间戳
    # 新增字段
    dir: str             # 相对路径下的一级子目录；根目录下则为 ""
    duration: Optional[float] = None  # 秒

    def to_dict(self) -> dict:
        return asdict(self)


def _get_ffprobe() -> Optional[str]:
    """获取 imageio-ffmpeg 提供的 ffprobe 路径"""
    try:
        import imageio_ffmpeg
        # imageio_ffmpeg 在 0.5+ 提供 get_ffmpeg_exe；旧版用 get_ffmpeg
        if hasattr(imageio_ffmpeg, "get_ffmpeg_exe"):
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        else:
            ffmpeg = imageio_ffmpeg.get_ffmpeg()
        ffprobe = Path(ffmpeg).with_name(
            "ffprobe" + (ffmpeg[-4:] if ffmpeg.lower().endswith(".exe") else "")
        )
        if ffprobe.exists():
            return str(ffprobe)
        # 退化：使用 ffmpeg -i
        return ffmpeg
    except Exception:
        return None


_FFPROBE_BIN: Optional[str] = None


def _probe_duration(path: Path) -> Optional[float]:
    """探测视频时长（秒）。失败返回 None。"""
    global _FFPROBE_BIN
    if _FFPROBE_BIN is None:
        _FFPROBE_BIN = _get_ffprobe()
    if _FFPROBE_BIN is None:
        return None
    try:
        is_ffmpeg = _FFPROBE_BIN.endswith(("ffmpeg", "ffmpeg.exe"))
        if is_ffmpeg:
            # ffmpeg 没有专门显示 duration 的格式输出，借助 stderr
            proc = subprocess.run(
                [_FFPROBE_BIN, "-i", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            import re
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr or "")
            if m:
                h, mi, s = m.groups()
                return int(h) * 3600 + int(mi) * 60 + float(s)
            return None

        proc = subprocess.run(
            [
                _FFPROBE_BIN, "-v", "quiet",
                "-print_format", "json",
                "-show_format", str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
        dur = (data.get("format") or {}).get("duration")
        return float(dur) if dur else None
    except Exception:
        return None


class VideoScanner:
    def __init__(self, root: Optional[str] = None,
                 extensions: Optional[List[str]] = None,
                 recursive: Optional[bool] = None,
                 cache_ttl: Optional[int] = None,
                 probe_duration: Optional[bool] = None):
        self.root = Path(root or CONFIG.video.root).expanduser().resolve()
        self.extensions = tuple(e.lower() for e in (extensions or CONFIG.video.extensions))
        self.recursive = bool(CONFIG.video.recursive if recursive is None else recursive)
        self.cache_ttl = int(cache_ttl if cache_ttl is not None else CONFIG.video.cache_ttl)
        # 是否探测时长（首次扫描会比较慢，默认开启）
        self.probe_duration = bool(
            CONFIG.video.probe_duration if probe_duration is None else probe_duration
        )
        self._cache: List[VideoItem] = []
        self._cache_time: float = 0.0

    @staticmethod
    def _make_id(path: Path) -> str:
        return hashlib.md5(str(path).encode("utf-8")).hexdigest()[:16]

    def _iter_files(self) -> List[Path]:
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
        files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return files

    def scan(self, force: bool = False, force_probe: bool = False) -> List[VideoItem]:
        now = time.time()
        if not force and self._cache and (now - self._cache_time) < self.cache_ttl:
            return self._cache

        items: List[VideoItem] = []
        for fp in self._iter_files():
            try:
                st = fp.stat()
            except OSError:
                continue
            try:
                rel = fp.relative_to(self.root).as_posix()
            except ValueError:
                rel = fp.name
            # 一级子目录
            parts = rel.split("/")
            first_dir = parts[0] if len(parts) > 1 else ""

            duration: Optional[float] = None
            if self.probe_duration or force_probe:
                duration = _probe_duration(fp)

            items.append(VideoItem(
                id=self._make_id(fp),
                name=fp.stem,
                path=rel,
                full_path=str(fp),
                size=st.st_size,
                mtime=st.st_mtime,
                dir=first_dir,
                duration=duration,
            ))
        self._cache = items
        self._cache_time = now
        return items

    def get_by_id(self, video_id: str) -> Optional[VideoItem]:
        for item in self.scan():
            if item.id == video_id:
                return item
        return None

    def list_dirs(self) -> List[Dict]:
        """所有出现的一级子目录及其视频数"""
        counter: Dict[str, int] = {}
        for v in self.scan():
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
                 ) -> List[VideoItem]:
    """按子文件夹 + 时长筛选

    - dirs: None 或 [] 表示不限；否则只保留 dir 在列表内的视频；
            列表中传 "(根目录)" 表示只保留根目录下的视频。
    - max_seconds / min_seconds: 时长过滤（秒）
    """
    result = items
    if dirs:
        target = set(dirs)
        result = [v for v in result if (v.dir or "(根目录)") in target]
    if min_seconds is not None:
        result = [v for v in result if v.duration is not None and v.duration >= min_seconds]
    if max_seconds is not None:
        result = [v for v in result if v.duration is not None and v.duration <= max_seconds]
    return result
