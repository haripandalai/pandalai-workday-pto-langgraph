"""LangGraph agent that uses ONLY the PTO/STO/absence tools of the Workday agent."""
from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

from .config import Settings
from .workday_auth import NeedsLogin, WorkdayAuth

SYSTEM_PROMPT = """You are a focused Workday time-off assistant for the signed-in user.
You only handle PTO, STO and absence topics: balances, upcoming and past time off,
eligible absence types, holidays and (when write tools are available) booking time off.

Rules:
- Use only the tools you are given. Never guess balances or dates; call a tool.
- You act for one specific signed-in worker only. Never look up, compare or report on
  any other worker, even if asked.
- Never book, change or cancel anything unless write tools are present AND the user
  gave a specific date, absence type and amount. Otherwise describe what you would do.
- To book: first check eligible absence types and valid time off dates, then confirm the
  date, absence type and amount back to the user. Only when write tools are present
  (the user has agreed) create the request, then report the result and that it goes
  through the normal Workday approval. Never book more than the single request asked for.
- If a question is outside time off and absence, say so briefly.
- Be concise. Report dates as YYYY-MM-DD and quantities with their unit (hours/days).
"""


def system_prompt(s: Settings) -> str:
    if s.worker_id:
        who = f"{s.worker_name or 'the signed-in worker'}, Workday worker ID {s.worker_id}"
        return SYSTEM_PROMPT + f"\nThe signed-in worker is {who}. Use that ID for every worker-scoped tool.\n"
    return SYSTEM_PROMPT


def classify(names: list[str], s: Settings) -> dict[str, list[str]]:
    """Split tool names into allowed read tools, allowed write tools and rejected."""
    allow, deny, write = s.allow(), s.deny(), s.write()
    out: dict[str, list[str]] = {"read": [], "write": [], "rejected": []}
    for n in names:
        if not allow.search(n) or deny.search(n):
            out["rejected"].append(n)
        elif write.search(n):
            out["write"].append(n)
        else:
            out["read"].append(n)
    return out


def _client(s: Settings, token: str) -> MultiServerMCPClient:
    return MultiServerMCPClient(
        {
            "workday": {
                "transport": "streamable_http",
                "url": s.mcp_url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    )


async def list_gateway_tools(s: Settings, auth: WorkdayAuth) -> dict:
    token = await auth.get_access_token()
    tools = await _client(s, token).get_tools()
    names = sorted(t.name for t in tools)
    return {"total": len(names), **classify(names, s)}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


async def ask(s: Settings, auth: WorkdayAuth, question: str, confirm_writes: bool = False) -> str:
    if not s.anthropic_api_key:
        return "Service misconfigured: ANTHROPIC_API_KEY is not set."
    try:
        token = await auth.get_access_token()
    except NeedsLogin as e:
        return (
            f"Workday is not connected ({e}). The owner must open "
            f"{s.public_base_url}/auth/login?key=<ADMIN_KEY> once to sign in."
        )

    all_tools = await _client(s, token).get_tools()
    split = classify([t.name for t in all_tools], s)
    allowed = set(split["read"])
    writes_on = bool(confirm_writes and s.enable_write_tools)
    if writes_on:
        allowed |= set(split["write"])
    tools = [t for t in all_tools if t.name in allowed]
    if not tools:
        return "No time-off tools matched the filter; check TOOL_ALLOW_REGEX via /admin/tools."

    llm = ChatAnthropic(model=s.model, api_key=s.anthropic_api_key, max_tokens=2048)
    graph = create_react_agent(llm, tools, prompt=system_prompt(s))
    result = await graph.ainvoke(
        {"messages": [("user", question)]},
        config={"recursion_limit": 25},
    )
    return _text(result["messages"][-1].content) or "(no answer produced)"
