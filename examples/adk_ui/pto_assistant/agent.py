"""ADK dev-UI agent: one chat, two backends behind the Render service.

  ask_workday_agent  -> the custom agent hosted on Workday (Path 2, A2A)
  ask_time_off       -> our LangGraph agent driving Workday's tools (Path 1)

Run (from examples/adk_ui):   adk web --port 8090 .
Env: PTO_SERVICE_KEY (the service's SERVICE_KEY), GOOGLE_API_KEY, optional PTO_BASE_URL, ADK_MODEL.
"""
import os

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StreamableHTTPConnectionParams

BASE = os.environ.get("PTO_BASE_URL", "https://pandalai-workday-pto.onrender.com").rstrip("/")
KEY = os.environ.get("PTO_SERVICE_KEY", "").strip()


ALLOW_WRITES = os.environ.get("ADK_ALLOW_WRITES", "").lower() == "true"  # opt-in; default is read-only


def force_read_only(tool, args, tool_context):
    """Unless ADK_ALLOW_WRITES=true, never let the model turn on writes from this UI."""
    if "confirm_writes" in args:
        args["confirm_writes"] = False
    return None


READ_ONLY_NOTE = "- This chat is read-only: do not book or change anything."
BOOKING_NOTE = (
    "- Booking is allowed, one request at a time, in two steps. Step 1 (check): call ask_workday_agent with "
    "confirm_writes=false and a QUESTION, for example 'Check whether <date> is a valid time off date for me and "
    "which absence type fits <amount> of <type>.' (the service refuses booking verbs unless confirm_writes is "
    "true). Then show the user the exact date, absence type and amount and ask them to confirm. Step 2: ONLY "
    "after the user's explicit 'yes' in a later message, call ask_workday_agent again with confirm_writes=true "
    "and the same context_id, and the message 'Yes, go ahead and submit the request: <type>, <amount>, <date>.' "
    "Never submit without that explicit yes, never more than one request, never guess the date, type or amount. "
    "If Workday answers with a content-safety refusal or asks for approval, report that to the user and stop; "
    "do not retry with different wording."
)


root_agent = LlmAgent(
    name="pto_assistant",
    model=os.environ.get("ADK_MODEL", "gemini-3.8-flash"),
    instruction=(
        "You help with PTO, STO and time off questions by calling tools; never guess balances or dates.\n"
        "- Default to ask_workday_agent (the agent hosted on Workday).\n"
        "- If the user says 'path 1', 'langgraph' or names ask_time_off, use ask_time_off instead.\n"
        "- If the user asks to compare, call both with the same question and show the answers side by side, "
        "saying which tool produced which.\n"
        "- Pass the user's question through unchanged. To continue a Workday-agent conversation, pass the "
        "context_id from its last answer.\n"
        + (BOOKING_NOTE if ALLOW_WRITES else READ_ONLY_NOTE)
    ),
    tools=[
        McpToolset(connection_params=StreamableHTTPConnectionParams(url=f"{BASE}/mcp/{KEY}", timeout=300, sse_read_timeout=300))
    ],
    before_tool_callback=None if ALLOW_WRITES else force_read_only,
)
