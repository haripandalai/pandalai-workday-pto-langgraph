"""Call the Workday-hosted custom agent (pto-agent) from outside Workday.

Uses the OAuth client of the "PTO Workday Agent Bridge" external agent (authorization code + PKCE).
The redirect URI does not need to be reachable: you sign in, the browser lands on an error page, and
you copy that page's URL (it carries ?code=...) into a file. Secrets and tokens are never printed.

  export BRIDGE_CLIENT_ID=$(cat ~/.bridge_client_id)
  export BRIDGE_CLIENT_SECRET=$(cat ~/.bridge_client_secret)
  export BRIDGE_REDIRECT="https://localhost:8765/callback"       # exactly what the agent has registered

  python examples/path2/bridge_client.py login     # prints/opens the sign-in URL (sign in as lmcneil)
  # copy the URL of the page you land on, then:   pbpaste > ~/.bridge_code_url
  python examples/path2/bridge_client.py finish    # exchanges the code, saves ~/.bridge_token (4h)
  python examples/path2/bridge_client.py tools     # list MCP tools and their input schemas
  python examples/path2/bridge_client.py ask "List my time off between 2026-01-01 and 2027-12-31"
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

HOST = os.environ.get("WORKDAY_AGENT_HOST", "https://odp.agent.workday.com").rstrip("/")
TENANT = os.environ.get("WORKDAY_TENANT", "wxqmdmpnjn_extdev")
REDIRECT = os.environ.get("BRIDGE_REDIRECT", "https://localhost:8765/callback")
MCP_URL = f"{HOST}/{TENANT}/mcp"
PKCE_FILE = Path.home() / ".bridge_pkce.json"
CODE_FILE = Path.home() / ".bridge_code_url"
TOKEN_FILE = Path.home() / ".bridge_token"


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def _client() -> tuple[str, str]:
    cid, secret = os.environ.get("BRIDGE_CLIENT_ID", "").strip(), os.environ.get("BRIDGE_CLIENT_SECRET", "").strip()
    if not (cid and secret):
        sys.exit("Set BRIDGE_CLIENT_ID and BRIDGE_CLIENT_SECRET (the bridge agent's Credentials tab).")
    return cid, secret


def _endpoints() -> tuple[str, str]:
    try:
        r = httpx.get(f"{HOST}/.well-known/oauth-authorization-server/{TENANT}", timeout=20)
        r.raise_for_status()
        m = r.json()
        return m["authorization_endpoint"], m["token_endpoint"]
    except Exception:
        return f"{HOST}/auth/authorize/{TENANT}", f"{HOST}/auth/oauth2/{TENANT}/token"


def login() -> None:
    cid, _ = _client()
    authorize, _t = _endpoints()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    _write_private(PKCE_FILE, json.dumps({"verifier": verifier, "state": state}))
    url = authorize + "?" + urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print("Sign in as lmcneil. After you allow, the browser shows an error page; copy that page's URL, then run:")
    print("  pbpaste > ~/.bridge_code_url\n  python examples/path2/bridge_client.py finish")
    try:
        subprocess.run(["open", "-a", "Safari", url], check=True)
    except Exception:
        print("\nOpen this URL in a browser:\n" + url)


def finish() -> None:
    cid, secret = _client()
    _a, token_url = _endpoints()
    pk = json.loads(PKCE_FILE.read_text())
    q = parse_qs(urlparse(CODE_FILE.read_text().strip()).query)
    if q.get("error"):
        sys.exit(f"Sign-in failed: {q['error'][0]}")
    if q.get("state", [""])[0] != pk["state"] or "code" not in q:
        sys.exit("The pasted URL has no code, or its state does not match the last 'login'. Run login again.")
    r = httpx.post(token_url, data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
        "client_id": cid, "client_secret": secret, "code_verifier": pk["verifier"],
    }, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Token exchange failed: HTTP {r.status_code}")  # never echo the body
    body = r.json()
    _write_private(TOKEN_FILE, body["access_token"])
    CODE_FILE.unlink(missing_ok=True)
    PKCE_FILE.unlink(missing_ok=True)
    print(f"Signed in. Token saved to {TOKEN_FILE} (expires in about {body.get('expires_in', '?')}s).")


async def mcp(question: str | None) -> None:
    if not TOKEN_FILE.exists():
        sys.exit("No token. Run login, then finish.")
    token = TOKEN_FILE.read_text().strip()
    async with streamablehttp_client(MCP_URL, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print(f"{len(tools)} tool(s):")
            for t in tools:
                desc = (t.description or "").strip().splitlines()
                print(f" - {t.name}: {desc[0][:100] if desc else ''}")
                print("   input schema:", json.dumps(t.inputSchema)[:400])
            if question:
                if not tools:
                    sys.exit("No tools offered (check the agent's Available To group).")
                t = tools[0]
                key = next(iter((t.inputSchema or {}).get("properties", {})), None)
                if not key:
                    sys.exit("The tool takes no inputs; cannot pass a question.")
                print(f"\nCalling {t.name} with {key}=<question> ...")
                res = await s.call_tool(t.name, {key: question})
                for c in res.content:
                    print(getattr(c, "text", c))


AGENT_WID = os.environ.get("PTO_AGENT_WID", "eb029fba8fe110010540a7ca5b550000")  # Integration IDs > Workday ID
A2A_URL = f"{HOST}/v1/a2a/{TENANT}/{AGENT_WID}"


def a2a(cmd: str, question: str | None) -> None:
    """The route that works for a Workday-hosted custom agent: A2A (JSON-RPC), not MCP."""
    import uuid

    if not TOKEN_FILE.exists():
        sys.exit("No token. Run login, then finish.")
    h = {"Authorization": f"Bearer {TOKEN_FILE.read_text().strip()}"}
    if cmd == "card":
        r = httpx.get(f"{A2A_URL}/.well-known/agent-card.json", headers=h, timeout=30)
        print(f"HTTP {r.status_code}")
        print(json.dumps(r.json(), indent=1)[:3000] if r.status_code == 200 else r.text[:300])
        return
    if not question:
        sys.exit('Usage: bridge_client.py a2a "your question"')
    body = {"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {"message": {
        "kind": "message", "role": "user", "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": question}]}}}
    r = httpx.post(A2A_URL, headers=h, json=body, timeout=300)
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code}: {r.text[:300]}")
    res = r.json().get("result", {})
    print(f"state: {res.get('status', {}).get('state')}")
    for art in res.get("artifacts", []):
        for p in art.get("parts", []):
            print(p.get("text", ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["login", "finish", "tools", "ask", "card", "a2a"])
    ap.add_argument("question", nargs="?")
    a = ap.parse_args()
    if a.cmd == "login":
        login()
    elif a.cmd == "finish":
        finish()
    elif a.cmd in ("card", "a2a"):
        a2a(a.cmd, a.question)
    else:
        if a.cmd == "ask" and not a.question:
            sys.exit('Usage: bridge_client.py ask "your question"')
        asyncio.run(mcp(a.question if a.cmd == "ask" else None))
