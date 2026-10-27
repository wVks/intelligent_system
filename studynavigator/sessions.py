"""Anonymous browser workspaces; no accounts or model-controlled ownership."""

import hashlib
import re
import secrets
import uuid
from http.cookies import SimpleCookie

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

COOKIE_NAME = "study_session"


class BrowserSessionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)
        token = connection.cookies.get(COOKIE_NAME, "")
        fresh = not re.fullmatch(r"[a-f0-9]{64}", token)
        if fresh:
            token = secrets.token_hex(32)
        scope.setdefault("state", {})["workspace_id"] = hashlib.sha256(token.encode()).hexdigest()

        async def session_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if fresh:
                    cookie = SimpleCookie()
                    cookie[COOKIE_NAME] = token
                    cookie[COOKIE_NAME]["path"] = "/"
                    cookie[COOKIE_NAME]["httponly"] = True
                    cookie[COOKIE_NAME]["samesite"] = "Lax"
                    cookie[COOKIE_NAME]["max-age"] = 30 * 24 * 60 * 60
                    if connection.url.scheme == "https":
                        cookie[COOKIE_NAME]["secure"] = True
                    headers.append("set-cookie", cookie.output(header="").strip())
                if scope["path"].startswith(("/api/", "/sources/", "/materials/")):
                    headers["cache-control"] = "private, no-store"
                    headers.add_vary_header("Cookie")
            await send(message)

        # Browser mutations must originate from this app. API clients without Origin
        # still need the unguessable session cookie to address private records.
        origin = Headers(scope=scope).get("origin")
        expected = f"{connection.url.scheme}://{connection.url.netloc}"
        if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"} and origin and origin.rstrip("/") != expected:
            response = JSONResponse(status_code=403, content={
                "error": {"code": "invalid_origin", "message": "Отправьте запрос со страницы приложения."},
                "request_id": uuid.uuid4().hex[:12],
            })
            return await response(scope, receive, session_send)
        await self.app(scope, receive, session_send)
