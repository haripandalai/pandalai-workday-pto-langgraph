"""Call the Workday-hosted custom agent (pto-agent) over A2A (JSON-RPC message/send).

Workday runs the orchestration, the model and the tools; this module only forwards the question with
the OAuth token of the signed-in worker (see WorkdayAuth, the A2A client) and returns the answer text.
"""
from __future__ import annotations

import uuid

import httpx

from .config import Settings
from .workday_auth import NeedsLogin, WorkdayAuth

READ_ONLY_PREFIX = (
    "Read-only request: look things up and answer, but do not create, change, book or cancel "
    "anything. If the user asks for a change, say it needs explicit confirmation first.\n\n"
)


def build_request(question: str, context_id: str | None = None) -> dict:
    msg: dict = {
        "kind": "message",
        "role": "user",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": question}],
    }
    if context_id:
        msg["contextId"] = context_id
    return {"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {"message": msg}}


def parse_result(body: dict) -> tuple[str, str, str | None]:
    """Return (state, answer text, contextId) from a message/send response."""
    if body.get("error"):
        err = body["error"]
        return "error", f"Workday agent error {err.get('code')}: {err.get('message')}", None
    res = body.get("result", {})
    state = (res.get("status") or {}).get("state", "unknown")
    texts = [
        p.get("text", "")
        for art in res.get("artifacts", [])
        for p in art.get("parts", [])
        if p.get("kind", "text") == "text"
    ]
    if not texts:  # fall back to the last agent message in the history
        for m in reversed(res.get("history", [])):
            if m.get("role") == "agent":
                texts = [p.get("text", "") for p in m.get("parts", []) if p.get("kind", "text") == "text"]
                break
    return state, "\n".join(t for t in texts if t).strip(), res.get("contextId")


async def ask(
    s: Settings,
    auth: WorkdayAuth,
    question: str,
    *,
    confirm_writes: bool = False,
    context_id: str | None = None,
) -> str:
    if not s.pto_agent_wid:
        return "Service misconfigured: PTO_AGENT_WID is not set."
    try:
        token = await auth.get_access_token()
    except NeedsLogin as e:
        return (
            f"The Workday agent sign-in is not connected ({e}). The owner must open "
            f"{s.public_base_url}/auth/a2a/login?key=<ADMIN_KEY> once."
        )
    text = question if confirm_writes and s.enable_write_tools else READ_ONLY_PREFIX + question
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(
            s.a2a_url,
            headers={"Authorization": f"Bearer {token}"},
            json=build_request(text, context_id),
        )
    if r.status_code != 200:
        return f"The Workday agent returned HTTP {r.status_code}."
    state, answer, ctx = parse_result(r.json())
    if not answer:
        return f"(the Workday agent finished with state '{state}' and no text)"
    footer = f"\n\n[context_id: {ctx}]" if ctx else ""
    return answer + footer
