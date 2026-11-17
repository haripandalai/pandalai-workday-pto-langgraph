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

from . import a2a_agent, agent
from .config import get_settings
from .workday_auth import TokenStore, WorkdayAuth

settings = get_settings()
auth = WorkdayAuth(settings)
# Second sign-in: the bridge agent's OAuth client, used to call the Workday-hosted pto-agent over A2A.
a2a_auth = WorkdayAuth(
    settings,
    TokenStore(settings.a2a_token_path),
    client_id=settings.a2a_client_id,
    client_secret=settings.a2a_client_secret,
    redirect_uri=settings.a2a_redirect_uri,
)

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


@mcp.tool()
async def ask_workday_agent(question: str, confirm_writes: bool = False, context_id: str = "") -> str:
    """Ask the Workday-hosted time-off agent (Workday runs the reasoning and the tools).

    Same topics as ask_time_off: PTO/STO balances, booked time off, eligible absence types, holidays.
    confirm_writes: leave False. Set True ONLY after the user has explicitly agreed to a specific
    booking (date, type, amount). context_id: pass the id from a previous answer to continue that chat.
    """
    return await a2a_agent.ask(
        settings, a2a_auth, question, confirm_writes=confirm_writes, context_id=context_id or None
    )


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
    return JSONResponse({**auth.status(), "a2a": a2a_auth.status()})


async def a2a_login(request: Request):
    if not _admin_ok(request):
        return PlainTextResponse("not found", status_code=404)
    try:
        return RedirectResponse(await a2a_auth.login_url())
    except RuntimeError as e:
        return PlainTextResponse(str(e), status_code=500)


async def a2a_callback(request: Request):
    code, state = request.query_params.get("code"), request.query_params.get("state")
    if request.query_params.get("error"):
        return PlainTextResponse("Workday sign-in was not completed.", status_code=400)
    if not (code and state):
        return PlainTextResponse("missing code/state", status_code=400)
    try:
        await a2a_auth.exchange_code(code, state)
    except Exception as e:
        return PlainTextResponse(f"Sign-in failed: {e}", status_code=400)
    return HTMLResponse("<h3>Workday agent connected.</h3><p>You can close this tab.</p>")


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
        Route("/auth/a2a/login", a2a_login),
        Route("/auth/a2a/callback", a2a_callback),
        Route("/admin/tools", admin_tools),
    ],
    lifespan=lifespan,
)


# ---- top-level ASGI dispatcher: /mcp/<SERVICE_KEY> -> FastMCP --------------------
# SERVICE_KEY may contain '/', '+' or '=' (Render's generateValue is base64), so match it as
# a prefix of the path instead of splitting the path on '/'.
MCP_PREFIX = "/mcp/"


def _strip_key(path: str) -> str | None:
    """Return the remainder after /mcp/<SERVICE_KEY> ('' or '/...'), or None if the key is wrong."""
    key = settings.service_key
    tail = path[len(MCP_PREFIX):]
    if not key or not _same(tail[: len(key)], key):
        return None
    rest = tail[len(key):]
    return rest if rest == "" or rest.startswith("/") else None


async def app(scope, receive, send):
    if scope["type"] == "http" and scope["path"].startswith(MCP_PREFIX):
        rest = _strip_key(scope["path"])
        if rest is not None:
            new_path = "/mcp" + rest
            scope = dict(scope, path=new_path, raw_path=new_path.encode())
            return await mcp_app(scope, receive, send)
        resp = PlainTextResponse("not found", status_code=404)
        return await resp(scope, receive, send)
    return await web(scope, receive, send)
