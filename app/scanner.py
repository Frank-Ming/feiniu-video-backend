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
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
    duration: float | None = None    # 懒探测，可能为 None
    # 短剧元数据
    is_series: bool = False
    series_id: str = ""        # 同一部短剧的所有视频共用同一个 id
    episode_no: int = 0        # 当前集数（1 开始；非短剧为 0）
    series_count: int = 0     # 同剧总集数（仅 is_series 时有效）
    siblings: list[str] = field(default_factory=list)  # 同剧全部 id（按集数排序）

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------
_FFPROBE_BIN: str | None = None


def _cn_num(s: str) -> int:
    """中文数字转阿拉伯数字。最多支持 9999。"""
    cn_map = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
              "十": 10, "百": 100}
    if s.isdigit():
        return int(s)
    if not s:
        return 0
    total = 0
    cur = 0
    for ch in s:
        v = cn_map.get(ch, 0)
        if v >= 10:
            if cur == 0:
                cur = 1
            total += cur * v
            cur = 0
        else:
            cur = v
    total += cur
    return total


def _get_ffprobe() -> str | None:
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


def probe_duration(path: Path) -> float | None:
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


def probe_full_info(path: Path) -> dict | None:
    """探测视频详细信息（编码/帧率/分辨率/bitrate/时长），失败返回 None。

    优先用 ffprobe（json 格式更稳定）；没有 ffprobe 时用 ffmpeg 解析 stderr。
    返回字段:
      duration: 秒
      bitrate: bps
      size: 字节
      format_name: 容器格式 (e.g. "mov,mp4,m4a,3gp,3g2,mj2")
      video: { codec_name, codec_long_name, profile, width, height,
               avg_frame_rate, pix_fmt, bit_rate }
      audio: { codec_name, bit_rate, sample_rate, channels } 或 None
    """
    bin_path = _get_ffprobe()
    if bin_path is None:
        return None
    try:
        is_ffmpeg = bin_path.endswith(("ffmpeg", "ffmpeg.exe"))
        if is_ffmpeg:
            return _probe_via_ffmpeg(path, bin_path)
        proc = subprocess.run(
            [bin_path, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        return _parse_ffprobe_json(proc.stdout or "{}")
    except Exception:
        return None


def _parse_ffprobe_json(raw: str) -> dict | None:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return None
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    info: dict = {
        "duration": float(fmt["duration"]) if fmt.get("duration") else None,
        "bitrate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "size": int(fmt["size"]) if fmt.get("size") else None,
        "format_name": fmt.get("format_name"),
    }
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v:
        afr = v.get("avg_frame_rate") or "0/1"
        # "30/1" -> 30.0
        try:
            num, den = afr.split("/")
            fps = float(num) / float(den) if float(den) else 0.0
        except Exception:
            fps = None
        info["video"] = {
            "codec_name": v.get("codec_name"),
            "codec_long_name": v.get("codec_long_name"),
            "profile": v.get("profile"),
            "width": v.get("width"),
            "height": v.get("height"),
            "avg_frame_rate": fps,
            "pix_fmt": v.get("pix_fmt"),
            "bit_rate": int(v["bit_rate"]) if v.get("bit_rate") else None,
        }
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if a:
        info["audio"] = {
            "codec_name": a.get("codec_name"),
            "bit_rate": int(a["bit_rate"]) if a.get("bit_rate") else None,
            "sample_rate": int(a["sample_rate"]) if a.get("sample_rate") else None,
            "channels": a.get("channels"),
        }
    return info


def _probe_via_ffmpeg(path: Path, bin_path: str) -> dict | None:
    """没有 ffprobe 时用 ffmpeg -i 解析 stderr。"""
    try:
        proc = subprocess.run(
            [bin_path, "-i", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    out = (proc.stderr or "") + (proc.stdout or "")
    import re
    info: dict = {}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
    if m:
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    m = re.search(r"Duration:\s*\S+\s*,\s*start:\s*\S+\s*,\s*bitrate:\s*(\d+)\s*kb/s", out)
    if m:
        info["bitrate"] = int(m.group(1)) * 1000
    m = re.search(r"Video:\s*([^,]+),\s*([^,]+),[^,]*,\s*(\d+)x(\d+)[^,]*,\s*([\d.]+)\s*fps", out)
    if m:
        info["video"] = {
            "codec_name": m.group(1).strip(),
            "pix_fmt": m.group(2).strip(),
            "width": int(m.group(3)),
            "height": int(m.group(4)),
            "avg_frame_rate": float(m.group(5)),
            "profile": None,
            "codec_long_name": None,
            "bit_rate": None,
        }
    m = re.search(r"Audio:\s*([^,]+),[^,]*,\s*(\d+)\s*Hz[^,]*,\s*([^,]+)", out)
    if m:
        info["audio"] = {
            "codec_name": m.group(1).strip(),
            "sample_rate": int(m.group(2)),
            "channels": m.group(3).strip(),
            "bit_rate": None,
        }
    return info or None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
class VideoScanner:
    def __init__(self,
                 root: str | None = None,
                 extensions: list[str] | None = None,
                 recursive: bool | None = None,
                 cache_ttl: int | None = None):
        self.root = Path(root or CONFIG.video.root).expanduser().resolve()
        self.extensions = tuple(e.lower() for e in (extensions or CONFIG.video.extensions))
        self.recursive = bool(CONFIG.video.recursive if recursive is None else recursive)
        self.cache_ttl = int(cache_ttl if cache_ttl is not None else CONFIG.video.cache_ttl)

        # 缓存目录
        self.cache_dir = Path(__file__).resolve().parent.parent / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "videos.json"
        self.state_file = self.cache_dir / "scan_state.json"

        self._cache: list[VideoItem] = []
        self._cache_time: float = 0.0
        self._scan_lock = asyncio.Lock()
        self._scan_task: asyncio.Task | None = None
        self._scanning: bool = False

    @staticmethod
    def _make_id(path: Path) -> str:
        return hashlib.md5(str(path).encode("utf-8")).hexdigest()[:16]

    # ---- 持久化缓存 ----
    def _load_disk_cache(self) -> list[VideoItem]:
        if not self.cache_file.exists():
            return []
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return [VideoItem(**item) for item in raw]
        except Exception as e:
            logger.warning("读取缓存失败: %s", e)
            return []

    def _save_disk_cache(self, items: list[VideoItem]) -> None:
        try:
            self.cache_file.write_text(
                json.dumps([asdict(i) for i in items], ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("写缓存失败: %s", e)

    def _root_signature(self) -> dict | None:
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

    def _load_signature(self) -> dict | None:
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

    def _scan_sync(self) -> list[VideoItem]:
        if not self.root.exists():
            return []
        files: list[Path] = []
        if self.recursive:
            for p in self.root.rglob("*"):
                if p.is_file() and p.suffix.lower() in self.extensions:
                    files.append(p)
        else:
            for p in self.root.iterdir():
                if p.is_file() and p.suffix.lower() in self.extensions:
                    files.append(p)

        items: list[VideoItem] = []
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

        # 第二遍：识别短剧
        self._detect_series(items)
        return items

    def _detect_series(self, items: list[VideoItem]) -> None:
        """为每个视频计算短剧元数据。规则：
        1. 同目录下没有其他文件夹（避免与「电视剧/系列」混淆）
        2. 文件名是数字/集数（解析出 episode_no > 0）
        满足以上两条才算短剧；同目录下多个短剧视频归为同一部短剧。
        """
        from collections import defaultdict
        groups: dict[str, list[VideoItem]] = defaultdict(list)
        for v in items:
            d = str(Path(v.full_path).parent)
            groups[d].append(v)

        for dir_path, group in groups.items():
            parent = Path(dir_path)
            if not parent.exists() or self._sibling_has_subdir(parent):
                continue

            parsed: list[tuple[VideoItem, int]] = []
            for v in group:
                ep = self._parse_episode_no(v.name)
                if ep > 0:
                    parsed.append((v, ep))
            if len(parsed) < 2:
                continue

            # 去重：同一集数保留名字最「纯」的那个（中文「第N集」/「EPxx」> 纯数字）
            by_ep: dict[int, list[VideoItem]] = {}
            for v, ep in parsed:
                by_ep.setdefault(ep, []).append(v)

            def _purity(name: str) -> int:
                import re as _re
                if _re.search(r"第\s*[0-9一二三四五六七八九十]+\s*[集话話]", name):
                    return 0
                if _re.search(r"EP[\.\s]*[0-9]+", name, _re.IGNORECASE):
                    return 1
                if _re.fullmatch(r"[0-9]+", name):
                    return 2
                return 3

            deduped: list[VideoItem] = []
            for ep in sorted(by_ep.keys()):
                vs = sorted(by_ep[ep], key=lambda x: _purity(x.name))
                deduped.append(vs[0])

            deduped.sort(key=lambda x: self._parse_episode_no(x.name))
            count = len(deduped)

            # 稳定 series_id：基于「剧集所在目录的相对路径」
            try:
                rel_dir = parent.relative_to(self.root).as_posix()
            except ValueError:
                rel_dir = dir_path
            series_id = hashlib.md5(
                f"series:{rel_dir}".encode()
            ).hexdigest()[:16]
            ids = [v.id for v in deduped]
            for v in deduped:
                ep = self._parse_episode_no(v.name)
                v.is_series = True
                v.series_id = series_id
                v.episode_no = ep
                v.series_count = count
                v.siblings = ids

    @staticmethod
    def _parse_episode_no(name: str) -> int:
        """从文件名解析集数。识别「第1集」「EP02」「01」等格式，返回 1-based 集数；无法识别返回 0"""
        import re
        s = name.strip()
        # 中文：「第一集」「第1集」「第01集」「第一話」「第1話」
        m = re.search(r"第\s*([0-9０-９]+|[一二三四五六七八九十百零]+)\s*[集话話話]", s)
        if m:
            return _cn_num(m.group(1))
        # 英文：EP01 / E02 / EP.03 / Episode 4
        m = re.search(r"(?:EP|E)[\.\s]*([0-9]+)", s, re.IGNORECASE)
        if m:
            return int(m.group(1))
        # 纯数字文件名（注意：要先排除明显不是集数的，比如「1080p」「60fps」）
        # 要求：数字前面不是字母数字（避免匹配 "x1080"），后面必须是非字母数字且不是 'p' / 'x'
        m = re.search(r"(?<![A-Za-z0-9])([0-9]{1,4})(?![A-Za-z0-9pPx])", s)
        if m:
            return int(m.group(1))
        return 0

    @staticmethod
    def _sibling_has_subdir(directory: Path) -> bool:
        """判断同目录（不含子目录）下是否还有任何子目录"""
        try:
            for p in directory.iterdir():
                if p.is_dir():
                    return True
        except OSError:
            pass
        return False

    # ---- 对外 API ----
    def list_videos(self) -> list[VideoItem]:
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

    async def probe_duration_async(self, video_id: str) -> float | None:
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

    def get_by_id(self, video_id: str) -> VideoItem | None:
        for v in self.list_videos():
            if v.id == video_id:
                return v
        return None

    def delete_video(self, video_id: str) -> bool:
        """删除一个视频文件并从内存/磁盘缓存中移除。返回是否成功。"""
        item = self.get_by_id(video_id)
        if item is None:
            return False
        try:
            p = Path(item.full_path)
            if p.exists():
                p.unlink()
        except Exception:
            return False
        # 从内存 cache 移除
        self._cache = [v for v in self._cache if v.id != video_id]
        # 落盘
        try:
            self._save_disk_cache(self._cache)
        except Exception:
            pass
        return True

    def list_dirs(self) -> list[dict]:
        counter: dict[str, int] = {}
        for v in self.list_videos():
            d = v.dir or "(根目录)"
            counter[d] = counter.get(d, 0) + 1
        return [{"name": k, "count": v} for k, v in
                sorted(counter.items(), key=lambda x: -x[1])]

    def pick_random(self, max_size_bytes: int = 200 * 1024 * 1024,
                    exclude_ids: list[str] | None = None,
                    only_series_id: str | None = None,
                    only_first_episode: bool = False) -> VideoItem | None:
        """随机选一个视频。优先选 size <= max_size_bytes 的；可选排除/限定。

        only_first_episode=True 时只挑非 series 或 episode_no==1 的视频，
        避免用户随机时刷到短剧的中间集。
        """
        import random
        pool = self.list_videos()
        if exclude_ids:
            excl = set(exclude_ids)
            pool = [v for v in pool if v.id not in excl]
        if only_series_id:
            pool = [v for v in pool if v.series_id == only_series_id]
        if only_first_episode:
            pool = [v for v in pool
                    if (not v.is_series) or v.episode_no <= 1]
        small = [v for v in pool if v.size <= max_size_bytes]
        if small:
            return random.choice(small)
        if pool:
            return random.choice(pool)
        return None


# 全局单例
scanner = VideoScanner()


def filter_items(items: list[VideoItem],
                 dirs: list[str] | None = None,
                 max_seconds: float | None = None,
                 min_seconds: float | None = None,
                 require_duration: bool = False) -> list[VideoItem]:
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
