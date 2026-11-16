"""Render web service.

  claude.ai --MCP--> /mcp/<SERVICE_KEY>   (tool: ask_time_off)  --> LangGraph agent --> Workday agent MCP
  owner     --------> /auth/login?key=<ADMIN_KEY>  (one-time Workday sign-in as lmcneil)
"""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from . import agent
from .config import get_settings
from .workday_auth import WorkdayAuth

settings = get_settings()
auth = WorkdayAuth(settings)

# ---- MCP surface for claude.ai ---------------------------------------------
mcp = FastMCP(
    "pandalai-workday-pto",
    stateless_http=True,
    json_response=True,
    # Render serves us on its own hostname, so the localhost-only Host check must go.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def ask_time_off(question: str, confirm_writes: bool = False) -> str:
    """Ask the Workday time-off assistant (PTO, STO, absence) about the signed-in worker.

    Examples: "What are my PTO and STO balances?", "What time off do I have booked?",
    "Which absence types am I eligible for?".

    confirm_writes: leave False. Set True ONLY after the user has explicitly agreed to a
    specific booking (date, type, amount); otherwise the assistant stays read-only.
    """
    return await agent.ask(settings, auth, question, confirm_writes=confirm_writes)


mcp_app = mcp.streamable_http_app()  # must exist before session_manager is used


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and secrets.compare_digest(a.encode(), b.encode())


def _admin_ok(request: Request) -> bool:
    return _same(request.query_params.get("key", ""), settings.admin_key)


# ---- admin / auth routes ----------------------------------------------------
async def healthz(_: Request):
    return PlainTextResponse("ok")


async def auth_login(request: Request):
    if not _admin_ok(request):
        return PlainTextResponse("not found", status_code=404)
    return RedirectResponse(await auth.login_url())


async def auth_callback(request: Request):
    code, state = request.query_params.get("code"), request.query_params.get("state")
    if request.query_params.get("error"):
        return PlainTextResponse("Workday sign-in was not completed.", status_code=400)
    if not (code and state):
        return PlainTextResponse("missing code/state", status_code=400)
    try:
        await auth.exchange_code(code, state)
    except Exception as e:  # message is already sanitised in WorkdayAuth
        return PlainTextResponse(f"Sign-in failed: {e}", status_code=400)
    return HTMLResponse("<h3>Workday connected.</h3><p>You can close this tab.</p>")


async def auth_status(request: Request):
    if not _admin_ok(request):
        return PlainTextResponse("not found", status_code=404)
    return JSONResponse(auth.status())


async def admin_tools(request: Request):
    if not _admin_ok(request):
        return PlainTextResponse("not found", status_code=404)
    try:
        return JSONResponse(await agent.list_gateway_tools(settings, auth))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@asynccontextmanager
async def lifespan(_: Starlette):
    async with mcp.session_manager.run():
        yield


web = Starlette(
    routes=[
        Route("/healthz", healthz),
        Route("/auth/login", auth_login),
        Route("/auth/callback", auth_callback),
        Route("/auth/status", auth_status),
        Route("/admin/tools", admin_tools),
    ],
    lifespan=lifespan,
)


# ---- top-level ASGI dispatcher: /mcp/<SERVICE_KEY> -> FastMCP --------------------
async def app(scope, receive, send):
    if scope["type"] == "http" and scope["path"].startswith("/mcp/"):
        parts = scope["path"].split("/", 3)  # ['', 'mcp', '<key>', rest?]
        if len(parts) >= 3 and _same(parts[2], settings.service_key):
            rest = "/" + parts[3] if len(parts) == 4 and parts[3] else ""
            new_path = "/mcp" + rest
            scope = dict(scope, path=new_path, raw_path=new_path.encode())
            return await mcp_app(scope, receive, send)
        resp = PlainTextResponse("not found", status_code=404)
        return await resp(scope, receive, send)
    return await web(scope, receive, send)
