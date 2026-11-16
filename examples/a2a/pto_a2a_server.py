"""PTO agent served over A2A (Agent2Agent).

An ADK agent whose only tool is the Render LangGraph service's MCP tool `ask_time_off`,
published as an A2A server so other agents can discover and call it.

  PTO_SERVICE_KEY=... GOOGLE_API_KEY=... python examples/a2a/pto_a2a_server.py
  # agent card: http://localhost:8001/.well-known/agent-card.json

READ-ONLY by design: a before_tool_callback forces confirm_writes=False, so nothing can be
booked through this A2A endpoint no matter what a calling agent asks for.
"""
import os
import sys

import uvicorn
from google.adk.agents import LlmAgent
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StreamableHTTPConnectionParams

BASE = os.environ.get("PTO_BASE_URL", "https://pandalai-workday-pto.onrender.com").rstrip("/")
PORT = int(os.environ.get("PTO_A2A_PORT", "8001"))
MODEL = os.environ.get("ADK_MODEL", "gemini-3.8-flash")


def force_read_only(tool, args, tool_context):
    """Never allow write tools through this endpoint."""
    if tool.name == "ask_time_off":
        args["confirm_writes"] = False
    return None  # proceed with the (modified) call


def build_agent() -> LlmAgent:
    key = os.environ.get("PTO_SERVICE_KEY", "").strip()
    if not key:
        sys.exit("Set PTO_SERVICE_KEY (the service's SERVICE_KEY).")
    toolset = McpToolset(connection_params=StreamableHTTPConnectionParams(url=f"{BASE}/mcp/{key}"))
    return LlmAgent(
        name="pto_agent",
        model=MODEL,
        description=(
            "Answers questions about the signed-in worker's PTO and time off in Workday: balances, "
            "booked and upcoming time off dates, and eligible absence types. Read-only."
        ),
        instruction=(
            "Answer PTO, STO and time off questions by calling ask_time_off with the question. "
            "Return dates as YYYY-MM-DD with the weekday, and quantities with units. "
            "You cannot book or change time off; if asked, say so."
        ),
        tools=[toolset],
        before_tool_callback=force_read_only,
    )


app = None
if __name__ == "__main__":
    app = to_a2a(build_agent(), host="localhost", port=PORT)
    uvicorn.run(app, host="127.0.0.1", port=PORT)
