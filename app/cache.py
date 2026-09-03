"""内存缓存层。

- PersonalTokenCache：token「个人缓存」，按 user_id 缓存每个用户的 5idream token；
  同一用户再次登录自动刷新（旧 token 立即失效），支持空闲过期。
- LoginSessionStore：扫码登录会话（security_id / 轮询状态）。
- PageCache：按「用户 + 分类 + 页码 + 每页条数」缓存单页查询结果，
  并为每个 key 维护一把异步锁，防止并发翻页/刷新时重复请求 5idream（防击穿）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from dataclasses import dataclass
from typing import Any

from .config import settings


@dataclass
class TokenEntry:
    user_id: str
    token: str
    user: dict[str, Any]
    last_used: float


class PersonalTokenCache:
    """按用户(user_id)缓存 5idream token —— token 个人缓存。"""

    def __init__(self, idle_ttl: int = 3600):
        self._idle_ttl = idle_ttl
        self._by_user: dict[str, TokenEntry] = {}
        self._by_token: dict[str, str] = {}  # token -> user_id

    def put(self, user_id: str, token: str, user: dict[str, Any]) -> TokenEntry:
        old = self._by_user.get(user_id)
        if old:
            self._by_token.pop(old.token, None)
        entry = TokenEntry(user_id=user_id, token=token, user=user, last_used=time.time())
        self._by_user[user_id] = entry
        self._by_token[token] = user_id
        return entry

    def get_by_token(self, token: str) -> TokenEntry | None:
        user_id = self._by_token.get(token)
        if not user_id:
            return None
        entry = self._by_user.get(user_id)
        if not entry or entry.token != token:
            return None
        if self._is_expired(entry):
            self.remove(token)
            return None
        entry.last_used = time.time()
        return entry

    def get_by_user(self, user_id: str) -> TokenEntry | None:
        entry = self._by_user.get(user_id)
        if not entry:
            return None
        if self._is_expired(entry):
            self.remove(entry.token)
            return None
        entry.last_used = time.time()
        return entry

    def remove(self, token: str) -> None:
        user_id = self._by_token.pop(token, None)
        if user_id and self._by_user.get(user_id) and self._by_user[user_id].token == token:
            self._by_user.pop(user_id, None)

    def _is_expired(self, entry: TokenEntry) -> bool:
        return self._idle_ttl > 0 and (time.time() - entry.last_used) > self._idle_ttl

    def cleanup(self) -> int:
        stale = [
            token
            for token, user_id in self._by_token.items()
            if (entry := self._by_user.get(user_id)) and self._is_expired(entry)
        ]
        for token in stale:
            self.remove(token)
        return len(stale)

    def count(self) -> int:
        return len(self._by_user)


@dataclass
class LoginSession:
    session_id: str
    security_id: str
    qr_payload: str
    created_at: float
    deadline: float
    status: str = "pending"  # pending / success / expired / failed
    token: str | None = None
    user: dict[str, Any] | None = None
    error: str | None = None


class LoginSessionStore:
    """扫码登录会话。已完成/超时的会话保留一段时间后由后台清理。"""

    def __init__(self, ttl: int = 1800):
        self._ttl = ttl
        self._sessions: dict[str, LoginSession] = {}

    def create(self, security_id: str, qr_payload: str) -> LoginSession:
        session_id = uuid.uuid4().hex
        now = time.time()
        session = LoginSession(
            session_id=session_id,
            security_id=security_id,
            qr_payload=qr_payload,
            created_at=now,
            deadline=now + settings.login_timeout_seconds,
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> LoginSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.status == "pending" and time.time() > session.deadline:
            session.status = "expired"
        return session

    def cleanup(self) -> int:
        now = time.time()
        stale = [
            sid
            for sid, session in self._sessions.items()
            if now - session.created_at > self._ttl
        ]
        for sid in stale:
            self._sessions.pop(sid, None)
        return len(stale)


class PageCache:
    """分页查询结果的短期缓存。

    每个查询维度（用户+分类+页码+每页条数）独立缓存 + 独立异步锁：
    - 同一页被并发请求时只真正请求一次 5idream，其余等待并复用结果；
    - 翻页各自独立，互不阻塞。
    """

    def __init__(self, ttl: int = 60):
        self._ttl = ttl
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    @staticmethod
    def key_for(user_id: str, scope: str, category: str, page: int, page_size: int) -> str:
        return f"{user_id}:{scope}:{category}:{page}:{page_size}"

    def lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def get(self, key: str) -> dict[str, Any] | None:
        item = self._entries.get(key)
        if not item:
            return None
        ts, payload = item
        if self._ttl > 0 and time.time() - ts > self._ttl:
            self._entries.pop(key, None)
            return None
        return payload

    def put(self, key: str, payload: dict[str, Any]) -> None:
        if self._ttl <= 0:
            return
        self._entries[key] = (time.time(), payload)

    def invalidate_user(self, user_id: str) -> None:
        prefix = f"{user_id}:"
        stale = [key for key in self._entries if key.startswith(prefix)]
        for key in stale:
            self._entries.pop(key, None)

    def cleanup(self) -> int:
        removed = 0
        if self._ttl > 0:
            now = time.time()
            stale = [key for key, (ts, _) in self._entries.items() if now - ts > self._ttl]
            for key in stale:
                self._entries.pop(key, None)
            removed += len(stale)
        # 每个请求/等待者都会持有锁的强引用；WeakValueDictionary 会在
        # 完全无人持有/等待后自动移除锁，避免在 release->waiter resume
        # 的短暂窗口误删仍有等待者的锁。
        return removed
