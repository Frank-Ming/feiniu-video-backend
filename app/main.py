"""FastAPI 主程序：扫描 + 流媒体 + 用户系统 + 观看记录"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .config import CONFIG
from .scanner import scanner, filter_items, probe_duration, _get_ffprobe
from .users import user_store

# ---- 日志 ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("feiniu")


# ---- 生命周期：启动时 warm up，结束时清理 ----
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：尽量用磁盘缓存秒开，然后异步扫描校验
    await scanner.warm_up()
    yield
    # 关闭时无需特殊处理


app = FastAPI(title="飞牛短视频后端", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


# 注：扩展名 fallback；流式响应时优先用 ffprobe 异步探测真实 MIME（带内存缓存）
VIDEO_EXT_TO_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
}


# ---------- 鉴权 ----------
async def current_user(authorization: Optional[str] = Header(None)) -> Optional[str]:
    """从 Authorization: Bearer <token> 解析当前用户名；未登录返回 None"""
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    return user_store.whoami(token)


async def require_user(authorization: Optional[str] = Header(None)) -> str:
    username = await current_user(authorization)
    if not username:
        raise HTTPException(status_code=401, detail="未登录")
    return username


# ---------- 健康 & 配置 ----------
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "root": str(scanner.root),
        "scanning": scanner.is_scanning,
        "cache_size": len(scanner.list_videos()),
    }


@app.get("/api/config")
def get_config():
    return {
        "root": str(scanner.root),
        "recursive": scanner.recursive,
        "cache_ttl": scanner.cache_ttl,
        "server_port": CONFIG.server.port,
        "scanning": scanner.is_scanning,
    }


# ---------- 用户系统 ----------
@app.post("/api/auth/register")
def register(payload: dict):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")
    user = user_store.register(username, password)
    if not user:
        raise HTTPException(status_code=400, detail="用户名已存在或密码过短")
    token = user_store.login(username, password)
    return {"token": token, "username": username}


@app.post("/api/auth/login")
def login(payload: dict):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    token = user_store.login(username, password)
    if not token:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": token, "username": username}


@app.post("/api/auth/logout")
def logout(username: str = Depends(require_user),
           authorization: Optional[str] = Header(None)):
    if authorization and authorization.lower().startswith("bearer "):
        user_store.logout(authorization[7:].strip())
    return {"ok": True}


@app.get("/api/auth/me")
def me(username: str = Depends(current_user)):
    if not username:
        return {"username": None}
    user = user_store.users.get(username)
    created_at = user.created_at if user else None
    return {"username": username, "created_at": created_at}


# ---------- 视频 ----------
@app.get("/api/dirs")
def list_dirs(refresh: bool = Query(False)):
    """如果还没扫过，先确保扫一次（不阻塞）"""
    if refresh:
        # 后台异步重扫
        import asyncio
        asyncio.create_task(scanner.schedule_scan(force=True))
    return {
        "total": len(scanner.list_videos()),
        "scanning": scanner.is_scanning,
        "dirs": scanner.list_dirs(),
    }


@app.get("/api/videos")
async def list_videos(
    refresh: bool = Query(False, description="是否强制刷新扫描缓存"),
    dir: Optional[List[str]] = Query(None),
    dirs: Optional[str] = Query(None),
    max_seconds: Optional[float] = Query(None, ge=0),
    min_seconds: Optional[float] = Query(None, ge=0),
    limit: Optional[int] = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    # 若请求 refresh=1 触发后台重扫，但不阻塞
    if refresh:
        import asyncio
        asyncio.create_task(scanner.schedule_scan(force=True))

    # 首次访问：如果磁盘有缓存就立即返回（秒开），没缓存则同步等待扫描完
    if not scanner.list_videos():
        await scanner.ensure_scanned()
    # 同时后台再扫一次（保证以后请求都新鲜）
    import asyncio
    asyncio.create_task(scanner.schedule_scan(force=False))

    items = scanner.list_videos()

    # 解析 dirs
    final_dirs: Optional[List[str]] = None
    if dir:
        final_dirs = dir
    elif dirs:
        final_dirs = [s.strip() for s in dirs.split(",") if s.strip()] or None

    filtered = filter_items(
        items,
        dirs=final_dirs,
        max_seconds=max_seconds,
        min_seconds=min_seconds,
    )
    total = len(filtered)
    page = filtered[offset: offset + (limit or total)]
    return {
        "total": total,
        "scan_total": len(items),
        "count": len(page),
        "scanning": scanner.is_scanning,
        "filter": {
            "dirs": final_dirs,
            "min_seconds": min_seconds,
            "max_seconds": max_seconds,
        },
        "videos": [v.to_dict() for v in page],
    }


@app.get("/api/videos/{video_id}")
async def get_video(video_id: str):
    item = scanner.get_by_id(video_id)
    if not item:
        raise HTTPException(status_code=404, detail="video not found")
    return item.to_dict()


@app.get("/api/random")
async def pick_random(
    max_size_mb: int = Query(200, ge=1, le=10240,
                            description="最大文件大小（MB）；超过的视频不会出现在随机池中"),
    exclude: Optional[List[str]] = Query(None,
                                          description="要排除的视频 id 列表"),
    series_id: Optional[str] = Query(None,
                                     description="限定在某个短剧内随机（自动连播下一集时用）"),
    username: str = Depends(current_user),   # 不强制登录，但登录后可避免随机到「不感兴趣」的
):
    """登录后随机挑一个视频；要求文件 < max_size_mb 以保证加载快"""
    if not scanner.list_videos():
        await scanner.ensure_scanned()
    item = scanner.pick_random(
        max_size_bytes=max_size_mb * 1024 * 1024,
        exclude_ids=exclude,
        only_series_id=series_id,
    )
    if not item:
        raise HTTPException(status_code=404, detail="no videos available")
    return item.to_dict()


@app.get("/api/series/{series_id}")
async def get_series(series_id: str):
    """返回某部短剧的全部视频（按集数排序）"""
    items = [v for v in scanner.list_videos()
             if v.series_id == series_id]
    if not items:
        raise HTTPException(status_code=404, detail="series not found")
    items.sort(key=lambda x: x.episode_no)
    return {
        "series_id": series_id,
        "count": len(items),
        "videos": [v.to_dict() for v in items],
    }


# ---------- MIME 真实探测（缓存到内存） ----------
_MIME_CACHE: dict[str, str] = {}


def _detect_mime(fp: Path) -> str:
    """优先按扩展名 fallback；扩展名为 .mp4 但内容不是 mp4 时，用 ffprobe 探测真实容器"""
    fallback = VIDEO_EXT_TO_MIME.get(fp.suffix.lower(), "application/octet-stream")
    key = str(fp.resolve())
    if key in _MIME_CACHE:
        return _MIME_CACHE[key]
    # 只对常见视频扩展名做探测（MPEG-TS 经常被错误命名为 .mp4）
    if fp.suffix.lower() not in (".mp4", ".m4v", ".mov", ".mkv"):
        _MIME_CACHE[key] = fallback
        return fallback
    try:
        bin_path = _get_ffprobe()
        if bin_path is None:
            _MIME_CACHE[key] = fallback
            return fallback
        # 只解析 format 段，看 format_name
        proc = subprocess.run(
            [bin_path, "-v", "quiet", "-print_format", "json",
             "-show_format", str(fp)],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout or "{}")
            fmt_name = ((data.get("format") or {}).get("format_name", "") or "").lower()
            # 映射 ffprobe format_name → 标准 MIME
            # 注意：ffmpeg 对 mp4/mov 文件会同时报 'mov,mp4,m4a,3gp,3g2,mj2'
            # 所以优先精确匹配再 fallback
            if "mpegts" in fmt_name:
                mime = "video/mp2t"
            elif "matroska" in fmt_name or "webm" in fmt_name:
                mime = "video/webm" if "webm" in fmt_name else "video/x-matroska"
            elif "mp4" in fmt_name or "mov" in fmt_name:
                # 扩展名是 .mp4 但格式名里有 mpegts 的已经被上面拦下
                mime = "video/mp4"
            elif "avi" in fmt_name:
                mime = "video/x-msvideo"
            elif "flv" in fmt_name:
                mime = "video/x-flv"
            else:
                mime = fallback
            _MIME_CACHE[key] = mime
            return mime
    except Exception:
        pass
    _MIME_CACHE[key] = fallback
    return fallback


@app.post("/api/videos/{video_id}/probe")
async def probe_one(video_id: str):
    """按 id 探测时长（懒探测接口）"""
    item = scanner.get_by_id(video_id)
    if not item:
        raise HTTPException(status_code=404, detail="video not found")
    d = await scanner.probe_duration_async(video_id)
    return {"id": video_id, "duration": d}


# ---------- 观看记录 ----------
@app.get("/api/history")
def get_history(limit: int = Query(200, ge=1, le=1000),
                username: str = Depends(require_user)):
    return {"history": user_store.list_history(username, limit=limit)}


@app.post("/api/history")
def report_history(payload: dict, username: str = Depends(require_user)):
    video_id = payload.get("video_id")
    position = payload.get("position")
    duration = payload.get("duration")
    if video_id is None or position is None or duration is None:
        raise HTTPException(status_code=400, detail="缺少必要字段")
    item = scanner.get_by_id(video_id)
    user_store.report_progress(
        username, video_id, float(position), float(duration),
        name=item.name if item else None,
        dir=item.dir if item else None,
    )
    return {"ok": True}


@app.get("/api/history/{video_id}")
def get_one_history(video_id: str, username: str = Depends(require_user)):
    r = user_store.get_progress(username, video_id)
    return r or {}


# ---------- 流媒体 ----------
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _iter_range(file_path: Path, start: int, end: int, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    with file_path.open("rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = f.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


@app.get("/api/stream/{video_id}")
def stream_video(video_id: str, request: Request):
    item = scanner.get_by_id(video_id)
    if not item:
        raise HTTPException(status_code=404, detail="video not found")

    fp = Path(item.full_path)
    if not fp.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")

    file_size = fp.stat().st_size
    mime = _detect_mime(fp)

    range_header = request.headers.get("range") or request.headers.get("Range")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{fp.name}"',
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
    }

    if range_header:
        m = _RANGE_RE.match(range_header)
        if not m:
            raise HTTPException(status_code=416, detail="invalid range")
        start_s, end_s = m.group(1), m.group(2)
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else file_size - 1
        if end >= file_size:
            end = file_size - 1
        if start > end or start < 0:
            raise HTTPException(status_code=416, detail="invalid range")
        length = end - start + 1
        headers.update({
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
        })
        return StreamingResponse(
            _iter_range(fp, start, end),
            status_code=206,
            media_type=mime,
            headers=headers,
        )

    headers["Content-Length"] = str(file_size)

    def _full() -> Iterator[bytes]:
        with fp.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(_full(), media_type=mime, headers=headers)


@app.exception_handler(Exception)
async def _err(_: Request, exc: Exception):
    logger.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def run() -> None:
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=CONFIG.server.host,
        port=CONFIG.server.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
