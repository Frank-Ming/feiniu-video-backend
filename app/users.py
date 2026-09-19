"""用户系统 + 观看记录

设计：
- 用户数据存 JSON 文件，密码 hash 用 SHA256 + salt
- 登录后下发一个长期 token（自校验）
- 观看记录按用户分组，每个用户每个视频保留最后进度
- 账号永久保留，除非超管在后台显式删除（删除时同步清历史和 session）
- 首次部署时自动种子 www 超管 + Frank 普通用户
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_FILE = DATA_DIR / "users.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"

# 种子账号：仅在 users.json 不存在时创建
SEED_ADMIN_USERNAME = "www"
SEED_ADMIN_PASSWORD = "1qaz!QAZ"
SEED_USER_USERNAME = "Frank"
SEED_USER_PASSWORD = "1qaz1QAZ"


def _hash_pwd(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()


@dataclass
class User:
    id: str
    username: str
    salt: str
    password_hash: str
    created_at: float
    is_admin: bool = False
    last_login_at: float = 0.0
    can_delete: bool = False  # 客户端是否有删除视频权限
    # 用户偏好（每用户独立的播放设置）
    default_speed: float = 1.0
    default_volume: float = 1.0


class UserStore:
    def __init__(self):
        self.users: dict[str, User] = {}  # username -> User
        self.sessions: dict[str, str] = {}  # token -> username
        self._history: dict[str, list[dict]] = {}  # username -> list of records
        self._load()
        self._seed_if_empty()

    # ---------- 持久化 ----------
    def _load(self):
        if USERS_FILE.exists():
            try:
                raw = json.loads(USERS_FILE.read_text(encoding="utf-8"))
                for u in raw.get("users", []):
                    # 兼容老数据：缺字段用 dataclass 默认值
                    self.users[u["username"]] = User(id=u.get("id") or secrets.token_hex(8), username=u["username"], salt=u.get("salt") or secrets.token_hex(8), password_hash=u.get("password_hash", ""), created_at=u.get("created_at", 0.0), is_admin=bool(u.get("is_admin", False)), last_login_at=u.get("last_login_at", 0.0), can_delete=bool(u.get("can_delete", False)), default_speed=u.get("default_speed", 1.0), default_volume=u.get("default_volume", 1.0))
            except Exception as e:
                log.warning("加载 users.json 失败: %s", e)
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

    def _seed_if_empty(self):
        """仅在没有任何用户时创建种子账号（首次部署）"""
        if self.users:
            return
        # 直接创建，不写 password_hash 函数外面调用
        try:
            self._create_user_internal(SEED_ADMIN_USERNAME, SEED_ADMIN_PASSWORD,
                                        is_admin=True)
            # Frank 默认给删除视频权限（用户希望快速浏览时不喜欢的直接删）
            frank = self._create_user_internal(
                SEED_USER_USERNAME, SEED_USER_PASSWORD, is_admin=False)
            frank.can_delete = True
            self._save_users()
            log.info("已创建种子账号: %s (超管), %s (普通用户，可删除视频)",
                     SEED_ADMIN_USERNAME, SEED_USER_USERNAME)
        except Exception as e:
            log.error("种子账号创建失败: %s", e)

    def _save_users(self):
        try:
            USERS_FILE.write_text(
                json.dumps(
                    {"users": [asdict(u) for u in self.users.values()]},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("保存 users.json 失败: %s", e)

    def _save_sessions(self):
        try:
            SESSIONS_FILE.write_text(
                json.dumps({"sessions": self.sessions}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("保存 sessions.json 失败: %s", e)

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
        except Exception as e:
            log.warning("保存 history_%s.json 失败: %s", username, e)

    # ---------- 用户 CRUD ----------
    def list_users(self) -> list[dict]:
        return [
            {
                "username": u.username,
                "created_at": u.created_at,
                "is_admin": u.is_admin,
                "last_login_at": u.last_login_at,
                "can_delete": u.can_delete,
            }
            for u in self.users.values()
        ]

    def get_user(self, username: str) -> User | None:
        return self.users.get(username)

    def is_admin(self, username: str) -> bool:
        u = self.users.get(username)
        return u.is_admin if u else False

    def user_can_delete(self, username: str) -> bool:
        """普通用户：返回 can_delete 标志；超管：永远 True"""
        u = self.users.get(username)
        if not u:
            return False
        if u.is_admin:
            return True
        return u.can_delete

    def _create_user_internal(self, username: str, password: str,
                               is_admin: bool = False) -> User:
        salt = secrets.token_hex(8)
        user = User(
            id=secrets.token_hex(8),
            username=username,
            salt=salt,
            password_hash=_hash_pwd(password, salt),
            created_at=time.time(),
            is_admin=is_admin,
        )
        self.users[username] = user
        self._load_history(username)
        return user

    def register(self, username: str, password: str,
                  is_admin: bool = False) -> User | None:
        """公开注册入口（普通用户注册）。超管账号只能通过后台创建。"""
        username = username.strip()
        if not username or not password:
            return None
        if username in self.users:
            return None
        if len(password) < 4:
            return None
        user = self._create_user_internal(username, password, is_admin=False)
        self._save_users()
        return user

    def admin_create_user(self, username: str, password: str,
                           is_admin: bool = False) -> User | None:
        """超管后台新增用户，允许任何合法字段值。"""
        username = (username or "").strip()
        if not username or not password:
            return None
        if username in self.users:
            return None
        if len(password) < 4:
            return None
        user = self._create_user_internal(username, password, is_admin=is_admin)
        self._save_users()
        return user

    def admin_update_user(self, username: str,
                           new_password: str | None = None,
                           is_admin: bool | None = None,
                           can_delete: bool | None = None) -> bool:
        """超管修改用户：重置密码 / 切换超管标志 / 切换删除权限。"""
        u = self.users.get(username)
        if not u:
            return False
        changed = False
        if new_password is not None and new_password != "":
            if len(new_password) < 4:
                return False
            u.salt = secrets.token_hex(8)
            u.password_hash = _hash_pwd(new_password, u.salt)
            changed = True
        if is_admin is not None and is_admin != u.is_admin:
            u.is_admin = is_admin
            changed = True
        if can_delete is not None and can_delete != u.can_delete:
            u.can_delete = can_delete
            changed = True
        if changed:
            self._save_users()
        return True

    def admin_delete_user(self, username: str) -> bool:
        """超管删除用户：同步清历史文件和该用户所有 session token。"""
        if username not in self.users:
            return False
        # 1. 删该用户的所有 session
        tokens_to_remove = [t for t, u in self.sessions.items() if u == username]
        for t in tokens_to_remove:
            self.sessions.pop(t, None)
        if tokens_to_remove:
            self._save_sessions()
        # 2. 删 history_<username>.json
        hf = self._history_file(username)
        if hf.exists():
            try:
                hf.unlink()
            except Exception as e:
                log.warning("删除历史文件失败: %s", e)
        self._history.pop(username, None)
        # 3. 删用户本身
        self.users.pop(username, None)
        self._save_users()
        return True

    def login(self, username: str, password: str) -> str | None:
        u = self.users.get(username)
        if not u:
            return None
        if u.password_hash != _hash_pwd(password, u.salt):
            return None
        token = secrets.token_hex(24)
        self.sessions[token] = username
        u.last_login_at = time.time()
        self._save_users()
        self._save_sessions()
        return token

    def logout(self, token: str):
        self.sessions.pop(token, None)
        self._save_sessions()

    def whoami(self, token: str) -> str | None:
        return self.sessions.get(token)

    # ---------- 观看记录 ----------
    def list_history(self, username: str, limit: int = 200) -> list[dict]:
        return list(self._history.get(username, []))[:limit]

    def report_progress(self, username: str, video_id: str,
                        position: float, duration: float,
                        name: str | None = None,
                        dir: str | None = None):
        """上报观看进度（按 video_id 合并，只留最新一条）"""
        if username not in self.users:
            return
        records = self._history.setdefault(username, [])
        now = time.time()
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
        if len(records) > 1000:
            records = records[:1000]
        self._history[username] = records
        self._save_history(username)

    def get_progress(self, username: str, video_id: str) -> dict | None:
        for r in self._history.get(username, []):
            if r["video_id"] == video_id:
                return r
        return None


# 全局单例
user_store = UserStore()
