"""有界的进程内键锁、缓存身份和日期工具。"""
from __future__ import annotations
import hashlib
import json
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from ..settings import get_settings

_locks = tuple(threading.RLock() for _ in range(64))

def identity(*values) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()

@contextmanager
def task_lock(key: str):
    from .llm.agent_runtime import check_active, remaining_seconds
    lock = _locks[int(identity(key)[:8], 16) % len(_locks)]
    if not lock.acquire(timeout=remaining_seconds()):
        raise TimeoutError("task_lock_timeout")
    try:
        check_active()
        yield
    finally:
        lock.release()

def file_version(path: str | None):
    if not path:
        return None
    file = Path(path).resolve()
    try:
        stat = file.stat()
        return str(file), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(file), None, None

def today_key() -> str:
    return datetime.now(ZoneInfo(get_settings().papergraph_timezone)).strftime("%Y-%m-%d")


from contextlib import asynccontextmanager
import asyncio
import weakref
_async_locks = weakref.WeakKeyDictionary()

@asynccontextmanager
async def async_task_lock(key: str):
    from .llm.agent_runtime import check_active, remaining_seconds
    locks = _async_locks.setdefault(asyncio.get_running_loop(), tuple(asyncio.Lock() for _ in range(64)))
    lock = locks[int(identity(key)[:8], 16) % len(locks)]
    await asyncio.wait_for(lock.acquire(), remaining_seconds(180))
    try:
        check_active()
        yield
    finally:
        lock.release()
