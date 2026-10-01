from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class UploadBodyLimitMiddleware:
    """Bound multipart bytes before Starlette spools an uploaded file to disk."""

    def __init__(self, app: ASGIApp, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        source_upload = scope["method"] == "POST" and scope["path"].startswith("/api/v1/sources")
        wiki_write = scope["method"] in {"POST", "PUT"} and scope["path"].startswith("/api/v1/wiki/pages")
        if not source_upload and not wiki_write:
            await self.app(scope, receive, send)
            return
        # UTF-8 content is bounded again by the file layer. JSON can expand a
        # character into a six-byte escape before Pydantic sees the document.
        max_body_bytes = 12 * 1024 * 1024 + 64 * 1024 if wiki_write else self.max_body_bytes
        size_detail = "Wiki 请求超过大小限制" if wiki_write else "文件超过上传大小限制"
        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                response = JSONResponse(status_code=400, content={"detail": "无效的请求大小"})
                await response(scope, receive, send)
                return
            if length < 0 or length > max_body_bytes:
                response = JSONResponse(status_code=413, content={"detail": size_detail})
                await response(scope, receive, send)
                return

        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_body_bytes:
                    raise HTTPException(status_code=413, detail=size_detail)
            return message

        await self.app(scope, bounded_receive, send)
