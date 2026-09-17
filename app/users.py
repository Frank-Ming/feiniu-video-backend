"""用户系统 + 观看记录

简化设计：
- 用户数据存 JSON 文件，密码 hash 用 SHA256 + salt
- 登录后下发一个长期 token（不引入 JWT 等依赖，简单自校验）
- 观看记录按用户分组，每个用户每个视频保留最后进度
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_FILE = DATA_DIR / "users.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"


def _hash_pwd(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


@dataclass
class User:
    id: str
    username: str
    salt: str
    password_hash: str
    created_at: float
    # 用户偏好（每用户独立的播放设置）
    default_speed: float = 1.0
    default_volume: float = 1.0


class UserStore:
    def __init__(self):
        self.users: Dict[str, User] = {}  # username -> User
        self.sessions: Dict[str, str] = {}  # token -> username
        self._history: Dict[str, List[dict]] = {}  # username -> list of records
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        if USERS_FILE.exists():
            try:
                raw = json.loads(USERS_FILE.read_text(encoding="utf-8"))
                for u in raw.get("users", []):
                    self.users[u["username"]] = User(**u)
            except Exception:
                self.users = {}
        if SESSIONS_FILE.exists():
            try:
                raw = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
                self.sessions = raw.get("sessions", {})
            except Exception:
                self.sessions = {}
        # 每用户独立历史文件
        for username in list(self.users.keys()):
            self._load_history(username)

    def _save_users(self):
        try:
            USERS_FILE.write_text(
                json.dumps(
                    {"users": [asdict(u) for u in self.users.values()]},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _save_sessions(self):
        try:
            SESSIONS_FILE.write_text(
                json.dumps({"sessions": self.sessions}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _history_file(self, username: str) -> Path:
        return DATA_DIR / f"history_{username}.json"

    def _load_history(self, username: str):
        f = self._history_file(username)
        if f.exists():
            try:
                self._history[username] = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                self._history[username] = []
        else:
            self._history[username] = []

    def _save_history(self, username: str):
        try:
            self._history_file(username).write_text(
                json.dumps(self._history.get(username, []), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ---------- 用户管理 ----------
    def list_users(self) -> List[dict]:
        return [{"username": u.username, "created_at": u.created_at} for u in self.users.values()]

    def register(self, username: str, password: str) -> Optional[User]:
        username = username.strip()
        if not username or not password:
            return None
        if username in self.users:
            return None
        if len(password) < 4:
            return None
        salt = secrets.token_hex(8)
        user = User(
            id=secrets.token_hex(8),
            username=username,
            salt=salt,
            password_hash=_hash_pwd(password, salt),
            created_at=time.time(),
        )
        self.users[username] = user
        self._load_history(username)
        self._save_users()
        return user

    def login(self, username: str, password: str) -> Optional[str]:
        u = self.users.get(username)
        if not u:
            return None
        if u.password_hash != _hash_pwd(password, u.salt):
            return None
        token = secrets.token_hex(24)
        self.sessions[token] = username
        self._save_sessions()
        return token

    def logout(self, token: str):
        self.sessions.pop(token, None)
        self._save_sessions()

    def whoami(self, token: str) -> Optional[str]:
        return self.sessions.get(token)

    # ---------- 观看记录 ----------
    def list_history(self, username: str, limit: int = 200) -> List[dict]:
        return list(self._history.get(username, []))[:limit]

    def report_progress(self, username: str, video_id: str,
                        position: float, duration: float,
                        name: Optional[str] = None,
                        dir: Optional[str] = None):
        """上报观看进度（按 video_id 合并，只留最新一条）"""
        if username not in self.users:
            return
        records = self._history.setdefault(username, [])
        now = time.time()
        # 找现有
        existing = None
        for i, r in enumerate(records):
            if r["video_id"] == video_id:
                existing = i
                break
        rec = {
            "video_id": video_id,
            "position": float(position),
            "duration": float(duration),
            "name": name or "",
            "dir": dir or "",
            "updated_at": now,
            "finished": duration > 0 and position / duration >= 0.95,
        }
        if existing is not None:
            records.pop(existing)
        records.insert(0, rec)
        # 限制最大记录数
        if len(records) > 1000:
            records = records[:1000]
        self._history[username] = records
        self._save_history(username)

    def get_progress(self, username: str, video_id: str) -> Optional[dict]:
        for r in self._history.get(username, []):
            if r["video_id"] == video_id:
                return r
        return None


# 全局单例
user_store = UserStore()
