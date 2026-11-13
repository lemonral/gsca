import asyncio
from contextlib import asynccontextmanager
from collections import defaultdict, deque
from pathlib import Path
import hashlib
import hmac
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings, access_secret
from .provider import OllamaProvider, ProviderError
from .runtime import AgentRunner
from .store import Store
from .tools import ToolRegistry, ToolError


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class Login(Input):
    token: str = Field(min_length=1, max_length=256)


class SessionInput(Input):
    title: str = Field(default="新会话", min_length=1, max_length=80)


class RunInput(Input):
    prompt: str = Field(min_length=1, max_length=12000)


class FileInput(Input):
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=65536)


class BodyLimitMiddleware:
    """Bound actual body bytes, including HTTP chunked uploads, before JSON parsing."""
    def __init__(self, app, limit=100000):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.limit:
                return await JSONResponse({"detail": "请求过大"}, status_code=413)(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


def create_app(settings=None, provider=None):
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    secret = access_secret(settings)
    store = Store(settings.data_dir)
    registry = ToolRegistry(settings.data_dir / "workspaces")
    provider = provider or OllamaProvider(settings)
    runner = AgentRunner(settings, store, registry, provider)
    attempts = defaultdict(deque)
    static = Path(__file__).parent / "static"

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        yield
        await runner.shutdown()

    app = FastAPI(title="General Agent", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.runner, app.state.registry = store, runner, registry

    def signature(value):
        return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def authenticated(request):
        bearer = request.headers.get("authorization", "")
        if bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:].encode(), secret.encode()):
            return True
        value = request.cookies.get("agent_session", "")
        try:
            stamp, digest = value.split(".", 1)
            return 0 <= time.time() - int(stamp) <= 43200 and hmac.compare_digest(digest, signature(stamp))
        except (ValueError, TypeError):
            return False

    @app.middleware("http")
    async def guard(request, call_next):
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "无效请求长度"}, status_code=400)
        if length < 0 or length > 100000:
            return JSONResponse({"detail": "请求过大"}, status_code=413)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            # Browser clients must use same-origin JSON; no cross-origin cookie writes.
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "不接受跨站请求"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "不接受跨站请求"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/health", "/api/login"}:
            if not authenticated(request):
                return JSONResponse({"detail": "请先输入访问码登录"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "会话或任务不存在"}, status_code=404)

    @app.exception_handler(ToolError)
    async def tool_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Never echo passwords or large file contents in validation errors.
        return JSONResponse({"detail": [{k: e[k] for k in ("type", "loc", "msg") if k in e}
                                        for e in exc.errors()]}, status_code=422)

    @app.get("/api/health")
    async def health(request: Request):
        return {"ok": True, "model": settings.model_name, "provider": "Ollama · 3184千问代理", "authenticated": authenticated(request), "version": "0.1.0"}

    @app.get("/api/model-health")
    async def model_health():
        try:
            return {"ok": True, "upstream": await provider.health()}
        except ProviderError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/login")
    async def login(data: Login, request: Request):
        ip = request.client.host if request.client else "local"
        recent = attempts[ip]
        now = time.monotonic()
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= 8:
            raise HTTPException(429, "尝试次数过多，请一分钟后重试")
        if not hmac.compare_digest(data.token.strip().encode(), secret.encode()):
            recent.append(now)
            raise HTTPException(401, "访问码不正确")
        recent.clear()
        stamp = str(int(time.time()))
        response = JSONResponse({"ok": True})
        response.set_cookie("agent_session", stamp + "." + signature(stamp), max_age=43200,
                            httponly=True, secure=request.url.scheme == "https", samesite="strict")
        return response

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie("agent_session")
        return response

    @app.get("/api/tools")
    async def tools():
        return [{"name": t["function"]["name"], "description": t["function"]["description"]} for t in registry.schemas]

    @app.get("/api/sessions")
    async def sessions():
        return store.list_sessions()

    @app.post("/api/sessions", status_code=201)
    async def create_session(data: SessionInput):
        if not data.title.strip():
            raise HTTPException(422, "标题不能为空")
        return store.create_session(data.title.strip())

    @app.get("/api/sessions/{session_id}")
    async def session(session_id: str):
        return {"session": store.get_session(session_id), "messages": store.messages(session_id), "runs": store.list_runs(session_id)}

    @app.post("/api/sessions/{session_id}/runs", status_code=202)
    async def start_run(session_id: str, data: RunInput):
        session = store.get_session(session_id)
        if not data.prompt.strip():
            raise HTTPException(422, "消息不能为空")
        if len(runner.tasks) >= 8:
            raise HTTPException(429, "待处理任务已满，请稍后重试")
        try:
            run = store.create_run(session_id, data.prompt.strip())
        except ValueError as exc:
            raise HTTPException(409, "当前会话已有任务正在执行") from None
        if session["title"] == "新会话":
            store.rename_session(session_id, data.prompt.strip()[:35])
        runner.submit(run)
        return run

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str):
        return {"run": store.get_run(run_id), "events": store.events(run_id)}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        store.get_run(run_id)
        runner.cancel(run_id)
        return store.get_run(run_id)

    def file_guard(session_id):
        store.get_session(session_id)
        if any(r["status"] in {"queued", "running"} for r in store.list_runs(session_id)):
            raise HTTPException(409, "请等待当前任务结束后上传文件")

    @app.get("/api/sessions/{session_id}/files")
    async def files(session_id: str, path: str = "."):
        store.get_session(session_id)
        return registry.execute("list_files", {"path": path}, session_id)

    @app.get("/api/sessions/{session_id}/files/content", response_class=PlainTextResponse)
    async def file_content(session_id: str, path: str):
        store.get_session(session_id)
        content = registry.execute("read_file", {"path": path}, session_id)["content"]
        return PlainTextResponse(content)

    @app.post("/api/sessions/{session_id}/files", status_code=201)
    async def upload(session_id: str, data: FileInput):
        file_guard(session_id)
        return registry.execute("write_file", data.model_dump(), session_id)

    @app.get("/")
    async def home():
        return FileResponse(static / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    @app.get("/app.js")
    @app.get("/static/app.js")
    async def javascript():
        return FileResponse(static / "app.js", media_type="application/javascript")

    @app.get("/style.css")
    @app.get("/static/style.css")
    async def css():
        return FileResponse(static / "style.css", media_type="text/css")

    app.add_middleware(BodyLimitMiddleware)
    return app


app = create_app()
