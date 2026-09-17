"""FastAPI 主程序：视频列表 + HTTP Range 流媒体代理 + 筛选"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .config import CONFIG
from .scanner import scanner, filter_items


app = FastAPI(title="飞牛短视频后端", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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


@app.get("/api/health")
def health():
    return {"status": "ok", "root": str(scanner.root)}


@app.get("/api/config")
def get_config():
    return {
        "root": str(scanner.root),
        "recursive": scanner.recursive,
        "cache_ttl": scanner.cache_ttl,
        "server_port": CONFIG.server.port,
        "probe_duration": scanner.probe_duration,
    }


def _parse_dirs(raw: Optional[str]) -> Optional[List[str]]:
    """支持 dirs=a,b,c 或 dir=a&dir=b"""
    if raw is None:
        return None
    parts = [s.strip() for s in raw.split(",") if s.strip()]
    return parts or None


@app.get("/api/dirs")
def list_dirs(refresh: bool = Query(False)):
    """返回一级子目录及其视频数，供前端展示筛选选项"""
    items = scanner.scan(force=refresh)
    counter: dict[str, int] = {}
    for v in items:
        d = v.dir or "(根目录)"
        counter[d] = counter.get(d, 0) + 1
    return {
        "total": len(items),
        "dirs": [
            {"name": k, "count": v}
            for k, v in sorted(counter.items(), key=lambda x: -x[1])
        ],
    }


@app.get("/api/videos")
def list_videos(
    refresh: bool = Query(False, description="是否强制刷新扫描缓存"),
    dir: Optional[List[str]] = Query(
        None, description="按一级子目录过滤，可多次传；传 \"(根目录)\" 表示根目录下视频"
    ),
    dirs: Optional[str] = Query(
        None, description="同上，逗号分隔。优先级低于 dir 参数"
    ),
    max_seconds: Optional[float] = Query(
        None, ge=0, description="只返回时长 <= 该秒数的视频"
    ),
    min_seconds: Optional[float] = Query(
        None, ge=0, description="只返回时长 >= 该秒数的视频"
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    items = scanner.scan(force=refresh)

    # 合并 dir / dirs 两种传参
    final_dirs: Optional[List[str]] = None
    if dir:
        final_dirs = dir
    elif dirs:
        final_dirs = _parse_dirs(dirs)

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
        "filter": {
            "dirs": final_dirs,
            "min_seconds": min_seconds,
            "max_seconds": max_seconds,
        },
        "videos": [v.to_dict() for v in page],
    }


@app.get("/api/videos/{video_id}")
def get_video(video_id: str):
    item = scanner.get_by_id(video_id)
    if not item:
        raise HTTPException(status_code=404, detail="video not found")
    return item.to_dict()


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


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@app.get("/api/stream/{video_id}")
def stream_video(video_id: str, request: Request):
    """支持 HTTP Range 的视频流，兼容 video_player / ExoPlayer"""
    item = scanner.get_by_id(video_id)
    if not item:
        raise HTTPException(status_code=404, detail="video not found")

    fp = Path(item.full_path)
    if not fp.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")

    file_size = fp.stat().st_size
    mime = VIDEO_EXT_TO_MIME.get(fp.suffix.lower(), "application/octet-stream")

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
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def run() -> None:
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=CONFIG.server.host,
        port=CONFIG.server.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
