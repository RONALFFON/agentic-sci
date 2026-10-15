"""LLM 服务层 —— OpenAI 兼容接口封装与 HelloAgents LLM 适配."""

import logging
import os
import asyncio
import anyio
import copy
import threading
import time
from typing import Any

from hello_agents import HelloAgentsLLM
from ...settings import get_settings

logger = logging.getLogger(__name__)

def normalize_openai_compatible_chat_messages(messages: Any) -> Any:
    if not isinstance(messages, list):
        return messages
    out: list[Any] = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        role = str(m.get("role") or "").strip().lower()
        content = m.get("content")
        if role == "summary":
            text = content if isinstance(content, str) else ("" if content is None else str(content))
            out.append({"role": "user", "content": ("[前文摘要]\n" + text).strip()})
            continue
        if role == "developer":
            text = content if isinstance(content, str) else ("" if content is None else str(content))
            nm = dict(m)
            nm["role"] = "system"
            nm["content"] = text
            out.append(nm)
            continue
        out.append(m)
    return out

_llm_instance: HelloAgentsLLM | None = None
_llm_lock = threading.Lock()


class PaperGraphLLM(HelloAgentsLLM):
    """实例级兼容适配，不修改 HelloAgents 的全局类或进程代理环境。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._client_lock = threading.Lock()

    def _options(self, kwargs):
        from .agent_runtime import remaining_seconds
        options = dict(kwargs)
        options.setdefault("temperature", self.temperature)
        if self.max_tokens:
            options.setdefault("max_tokens", self.max_tokens)
        options["timeout"] = min(float(options.get("timeout") or self.timeout), remaining_seconds(self.timeout))
        if "deepseek" in str(self.base_url or "").lower():
            extra = dict(options.get("extra_body") or {})
            extra.setdefault("thinking", {"type": "disabled"})
            options["extra_body"] = extra
        return options

    def _local_adapter(self, *, asynchronous=False):
        adapter = copy.copy(self._adapter)
        attr = "_async_client" if asynchronous else "_client"
        with self._client_lock:
            client = getattr(self._adapter, attr, None)
            if client is None:
                client = self._adapter.create_async_client() if asynchronous else self._adapter.create_client()
                if hasattr(client, "with_options"):
                    client = client.with_options(max_retries=0)
                setattr(self._adapter, attr, client)
        setattr(adapter, attr, client)
        return adapter

    def _call(self, method, messages, **kwargs):
        from .agent_runtime import _run_with_optional_timeout, check_active, request_id, record_llm_call
        options = self._options(kwargs)
        options.setdefault("temperature", self.temperature)
        if self.max_tokens:
            options.setdefault("max_tokens", self.max_tokens)
        started = time.monotonic()
        status = "error"
        usage = None
        try:
            adapter = self._local_adapter()
            result = _run_with_optional_timeout(lambda: getattr(adapter, method)(normalize_openai_compatible_chat_messages(messages), **options), options["timeout"])
            check_active()
            usage = getattr(result, "usage", None)
            if hasattr(usage, "model_dump"):
                usage = usage.model_dump()
            status = "success"
            return result
        finally:
            record_llm_call(status, usage)
            logger.info("llm_call request_id=%s method=%s model=%s status=%s duration_ms=%d usage=%s", request_id(), method, self.model, status, (time.monotonic()-started)*1000, usage)

    def invoke(self, messages, **kwargs):
        return self._call("invoke", messages, **kwargs)

    def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
        return self._call("invoke_with_tools", messages, tools=tools, tool_choice=tool_choice, **kwargs)

    async def ainvoke(self, messages, **kwargs):
        from .agent_runtime import run_sync
        return await run_sync(self.invoke, messages, **kwargs)

    async def ainvoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
        from .agent_runtime import run_sync
        return await run_sync(self.invoke_with_tools, messages, tools, tool_choice=tool_choice, **kwargs)

    def stream_invoke(self, messages, **kwargs):
        from .agent_runtime import execution_scope, check_active, _run_with_optional_timeout, remaining_seconds, record_llm_call, request_id
        options = self._options(kwargs)
        with execution_scope(options["timeout"]):
            stream = self._local_adapter().stream_invoke(normalize_openai_compatible_chat_messages(messages), **options)
            sentinel = object()
            started, status = time.monotonic(), "error"
            try:
                while True:
                    chunk = _run_with_optional_timeout(lambda: next(stream, sentinel), remaining_seconds())
                    if chunk is sentinel:
                        status = "success"
                        break
                    check_active()
                    yield chunk
            finally:
                try:
                    stream.close()
                except (ValueError, RuntimeError):
                    pass
                record_llm_call(status)
                logger.info("llm_call request_id=%s method=stream_invoke model=%s status=%s duration_ms=%d usage=missing", request_id(), self.model, status, (time.monotonic()-started)*1000)

    async def astream_invoke(self, messages, **kwargs):
        from .agent_runtime import execution_scope, check_active, request_id, record_llm_call
        options = self._options(kwargs)
        with execution_scope(options["timeout"]):
            from ..task_state import async_task_lock
            channel = hash(request_id() or id(messages)) % get_settings().agent_runtime_max_workers
            started, status = time.monotonic(), "error"
            try:
                async with async_task_lock(f"llm-stream:{channel}"):
                    with anyio.fail_after(options["timeout"]):
                        async for chunk in self._local_adapter(asynchronous=True).astream_invoke(normalize_openai_compatible_chat_messages(messages), **options):
                            check_active()
                            yield chunk
                        status = "success"
            finally:
                record_llm_call(status)
                logger.info("llm_call request_id=%s method=astream_invoke model=%s status=%s duration_ms=%d usage=missing", request_id(), self.model, status, (time.monotonic()-started)*1000)


def coerce_hello_agents_llm_output_to_str(out: Any) -> str:
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    for attr in ("content", "text"):
        v = getattr(out, attr, None)
        if isinstance(v, str):
            return v
    msg = getattr(out, "message", None)
    if msg is not None:
        c = getattr(msg, "content", None)
        if isinstance(c, str):
            return c
    choices = getattr(out, "choices", None)
    if isinstance(choices, list) and choices:
        m = getattr(choices[0], "message", None)
        if m is not None:
            c = getattr(m, "content", None)
            if isinstance(c, str):
                return c
    return str(out)

def is_llm_configured() -> bool:
    settings = get_settings()
    return bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("AIHUBMIX_API_KEY") or settings.openai_api_key)


def get_llm() -> HelloAgentsLLM:
    global _llm_instance
    with _llm_lock:
        if _llm_instance is None:
            settings = get_settings()
            key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("AIHUBMIX_API_KEY") or settings.openai_api_key
            if not key:
                raise RuntimeError("LLM 未配置：请设置 LLM_API_KEY")
            model = os.getenv("LLM_MODEL_ID") or os.getenv("OPENAI_MODEL") or os.getenv("AIHUBMIX_MODEL_ID") or settings.openai_model
            base = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or os.getenv("AIHUBMIX_BASE_URL") or settings.openai_base_url
            _llm_instance = PaperGraphLLM(api_key=key, model=model, base_url=base, timeout=settings.agent_runtime_default_timeout_sec)
            # OpenAI 兼容客户端使用独立 HTTP 配置，不删除进程的代理环境变量。
            from hello_agents.core.llm_adapters import OpenAIAdapter
            if isinstance(_llm_instance._adapter, OpenAIAdapter):
                import httpx
                from openai import OpenAI, AsyncOpenAI
                trust = settings.papergraph_httpx_trust_env and not settings.llm_disable_proxy
                _llm_instance._adapter._client = OpenAI(api_key=key, base_url=base, timeout=_llm_instance.timeout, max_retries=0, http_client=httpx.Client(trust_env=trust))
                _llm_instance._adapter._async_client = AsyncOpenAI(api_key=key, base_url=base, timeout=_llm_instance.timeout, max_retries=0, http_client=httpx.AsyncClient(trust_env=trust))
            logger.info("LLM initialized model=%s", model)
        return _llm_instance


async def close_llm() -> None:
    global _llm_instance
    with _llm_lock:
        llm, _llm_instance = _llm_instance, None
    if llm is None:
        return
    for attribute in ("_client", "_async_client"):
        client = getattr(llm._adapter, attribute, None)
        if client is not None:
            result = client.close()
            if hasattr(result, "__await__"):
                await result
