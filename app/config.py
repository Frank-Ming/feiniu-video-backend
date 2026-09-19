"""配置加载：优先使用环境变量，其次使用 config/config.yaml"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 6969


class VideoConfig(BaseModel):
    root: str = "/vol1/1000/视频/H"
    extensions: list[str] = [".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".flv", ".ts"]
    recursive: bool = True
    cache_ttl: int = 30
    # 是否扫描时探测视频时长（首次扫描会更慢，但能让手机端按时长筛选）
    probe_duration: bool = True


class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    video: VideoConfig = VideoConfig()


def load_config() -> AppConfig:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    data: dict = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    # 环境变量覆盖
    env_root = os.environ.get("VIDEO_ROOT")
    if env_root:
        data.setdefault("video", {})["root"] = env_root

    env_port = os.environ.get("SERVER_PORT")
    if env_port:
        data.setdefault("server", {})["port"] = int(env_port)

    return AppConfig.model_validate(data)


CONFIG = load_config()
