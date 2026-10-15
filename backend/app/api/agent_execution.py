"""保持整个 ASGI 请求（包括 SSE）的预算与取消状态。"""
from __future__ import annotations
import anyio
import logging
import time
from starlette.responses import JSONResponse
from ..services.llm.agent_runtime import execution_scope
from ..settings import get_settings

logger = logging.getLogger(__name__)

class AgentExecutionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False
        finished = False
        streaming = False
        begin = time.monotonic()
        with execution_scope(get_settings().agent_request_timeout_sec) as budget:
            async def receive_checked():
                message = await receive()
                if message["type"] == "http.disconnect":
                    budget.cancelled.set()
                return message
            async def send_checked(message):
                nonlocal started, finished, streaming
                if message["type"] == "http.response.start":
                    started = True
                    streaming = any(key.lower() == b"content-type" and b"text/event-stream" in value for key, value in message.get("headers", []))
                    message.setdefault("headers", []).append((b"x-request-id", budget.request_id.encode("ascii")))
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    finished = True
                await send(message)
            try:
                with anyio.fail_after(budget.remaining()):
                    await self.app(scope, receive_checked, send_checked)
            except TimeoutError:
                budget.cancelled.set()
                if not started:
                    await JSONResponse({"detail": "请求处理超时"}, status_code=504)(scope, receive, send_checked)
                elif not finished:
                    body = b'event: error\ndata: {"error":"search_agent_timeout","done":true}\n\n' if streaming else b""
                    await send_checked({"type": "http.response.body", "body": body, "more_body": False})
            finally:
                logger.info("request_usage request_id=%s metrics=%s", budget.request_id, budget.stats.snapshot())
                logger.info("request_end request_id=%s path=%s cancelled=%s duration_ms=%d", budget.request_id, scope.get("path"), budget.cancelled.is_set(), (time.monotonic()-begin)*1000)
