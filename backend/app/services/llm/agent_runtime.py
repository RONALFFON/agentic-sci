"""有界执行、请求预算与无状态 Agent 调用。取消等待不会强杀 Python 线程。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar
from uuid import uuid4

from hello_agents import SimpleAgent
from ...settings import get_settings
from .agent_config import papergraph_agent_config

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


@dataclass
class ExecutionStats:
    calls: int = 0
    failures: int = 0
    total_tokens: int = 0
    usage_missing: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self):
        with self.lock:
            return {key: getattr(self, key) for key in ("calls", "failures", "total_tokens", "usage_missing")}


@dataclass
class ExecutionBudget:
    deadline: float
    request_id: str
    parent: ExecutionBudget | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    stats: ExecutionStats = field(default_factory=ExecutionStats)

    def remaining(self) -> float:
        if self.cancelled.is_set():
            raise TimeoutError("agent_execution_cancelled")
        remaining = self.deadline - time.monotonic()
        if self.parent is not None:
            remaining = min(remaining, self.parent.remaining())
        if remaining <= 0:
            raise TimeoutError("agent_execution_deadline")
        return remaining


_budget: contextvars.ContextVar[ExecutionBudget | None] = contextvars.ContextVar("agent_budget", default=None)
_in_worker: contextvars.ContextVar[bool] = contextvars.ContextVar("agent_worker", default=False)
_pool: concurrent.futures.ThreadPoolExecutor | None = None
_slots: threading.BoundedSemaphore | None = None
_pool_lock = threading.Lock()


@contextmanager
def execution_scope(timeout_sec: float | None = None, *, request_id: str | None = None, independent: bool = False):
    parent = None if independent else _budget.get()
    timeout = float(timeout_sec or get_settings().agent_runtime_default_timeout_sec)
    scope = ExecutionBudget(time.monotonic() + timeout, request_id or (parent.request_id if parent else uuid4().hex), parent)
    if parent is not None:
        scope.stats = parent.stats
    token = _budget.set(scope)
    try:
        yield scope
    except BaseException:
        scope.cancelled.set()
        raise
    finally:
        _budget.reset(token)


def check_active() -> None:
    scope = _budget.get()
    if scope is not None:
        scope.remaining()


def remaining_seconds(default: float | None = None) -> float:
    maximum = float(default or get_settings().agent_runtime_default_timeout_sec)
    scope = _budget.get()
    return min(maximum, scope.remaining()) if scope else maximum


def request_id() -> str | None:
    scope = _budget.get()
    return scope.request_id if scope else None


def record_llm_call(status: str, usage=None):
    scope = _budget.get()
    if scope is None:
        return
    with scope.stats.lock:
        scope.stats.calls += 1
        scope.stats.failures += int(status != "success")
        if isinstance(usage, dict) and type(usage.get("total_tokens")) is int:
            scope.stats.total_tokens += usage["total_tokens"]
        else:
            scope.stats.usage_missing += 1


def _executor():
    global _pool, _slots
    with _pool_lock:
        if _pool is None:
            workers = get_settings().agent_runtime_max_workers
            _pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="papergraph-agent")
            _slots = threading.BoundedSemaphore(workers)
        return _pool, _slots


def shutdown_agent_runtime() -> None:
    global _pool, _slots
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
        _slots = None


def _submit(fn: Callable[[], _T]):
    executor, slots = _executor()
    # 已超时但尚未退出的线程也占用名额，不建立无界队列。
    if not slots.acquire(blocking=False):
        raise RuntimeError("agent_runtime_busy")
    context = contextvars.copy_context()

    def invoke():
        token = _in_worker.set(True)
        try:
            check_active()
            result = fn()
            check_active()
            return result
        finally:
            _in_worker.reset(token)
    try:
        future = executor.submit(context.run, invoke)
    except BaseException:
        slots.release()
        raise
    future.add_done_callback(lambda _: slots.release())
    return future


def _run_with_optional_timeout(fn: Callable[[], _T], timeout_sec: float | None) -> _T:
    with execution_scope(timeout_sec) as scope:
        if _in_worker.get():
            check_active()
            result = fn()
            check_active()
            return result
        future = _submit(fn)
        try:
            return future.result(timeout=scope.remaining())
        except concurrent.futures.TimeoutError as exc:
            scope.cancelled.set()
            future.cancel()
            raise TimeoutError("agent_task_timeout") from exc


async def run_sync(fn: Callable[..., _T], *args, timeout_sec: float | None = None, **kwargs) -> _T:
    """同步任务在有界池运行，取消后由共享 budget 阻止后续副作用。"""
    with execution_scope(timeout_sec) as scope:
        future = _submit(lambda: fn(*args, **kwargs))
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), scope.remaining())
        except BaseException:
            scope.cancelled.set()
            future.cancel()
            raise


def _exception_chain_predicate(exc: BaseException | None, pred) -> bool:
    seen: set[int] = set()
    for _ in range(12):
        if exc is None or id(exc) in seen:
            break
        seen.add(id(exc))
        if pred(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _task_failed_due_to_timeout(exc: BaseException) -> bool:
    return _exception_chain_predicate(exc, lambda e: isinstance(e, TimeoutError) or "timeout" in str(e).lower() or "timed out" in str(e).lower())


def is_retryable(exc: BaseException) -> bool:
    def predicate(error):
        status = getattr(error, "status_code", None)
        return status == 429 or (isinstance(status, int) and status >= 500) or isinstance(error, ConnectionError) or "connection" in type(error).__name__.lower()
    return not _task_failed_due_to_timeout(exc) and _exception_chain_predicate(exc, predicate)


def run_agent_task(*, task_name: str, agent_name: str, llm: Any, system_prompt: str,
                   user_prompt: str, timeout_sec: float | None = None, retries: int | None = None,
                   task_logger: logging.Logger | None = None) -> str:
    from .llm_service import coerce_hello_agents_llm_output_to_str
    log = task_logger or logger
    attempts = 1 + (get_settings().agent_runtime_default_retries if retries is None else max(0, retries))
    with execution_scope(timeout_sec):
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                def invoke():
                    agent = SimpleAgent(name=agent_name, llm=llm, system_prompt=system_prompt, config=papergraph_agent_config())
                    return agent.run(user_prompt)
                result = _run_with_optional_timeout(invoke, remaining_seconds(timeout_sec))
                log.info("agent_task request_id=%s task=%s status=success attempt=%d duration_ms=%d", request_id(), task_name, attempt + 1, (time.monotonic() - started) * 1000)
                return coerce_hello_agents_llm_output_to_str(result).strip()
            except Exception as exc:
                log.warning("agent_task request_id=%s task=%s status=error attempt=%d error=%s", request_id(), task_name, attempt + 1, type(exc).__name__)
                if _task_failed_due_to_timeout(exc):
                    raise TimeoutError(f"{task_name}_timeout") from exc
                if attempt + 1 == attempts or not is_retryable(exc):
                    raise RuntimeError(f"{task_name}_failed") from exc
                time.sleep(min(0.25 * 2 ** attempt, remaining_seconds() / 4))
    raise RuntimeError(f"{task_name}_failed")


def run_json_task(*, task_name: str, agent_name: str, llm: Any, system_prompt: str,
                  user_prompt: str, timeout_sec: float | None = None, retries: int | None = None,
                  default: dict[str, Any] | None = None, parse_fn: Callable | None = None,
                  output_model: Any = None, task_logger: logging.Logger | None = None) -> dict[str, Any]:
    from .structured_output import parse_json
    with execution_scope(timeout_sec):
        for repair in range(2):
            raw = run_agent_task(task_name=task_name, agent_name=agent_name, llm=llm,
                                 system_prompt=system_prompt, user_prompt=user_prompt,
                                 timeout_sec=remaining_seconds(timeout_sec), retries=retries, task_logger=task_logger)
            try:
                data = parse_fn(raw) if parse_fn else parse_json(raw)
                if not isinstance(data, dict):
                    raise ValueError("expected_json_object")
                if output_model:
                    data = output_model.model_validate(data).model_dump()
                return data
            except (ValueError, TypeError):
                if repair == 0:
                    user_prompt = "请重新输出符合指定字段类型和范围的完整 JSON 对象，不要代码围栏。\n" + user_prompt
                    continue
                logger.warning("agent_json_invalid request_id=%s task=%s", request_id(), task_name)
                if default is not None:
                    return dict(default)
                raise ValueError(f"{task_name}_invalid_output")
    raise ValueError(f"{task_name}_invalid_output")
