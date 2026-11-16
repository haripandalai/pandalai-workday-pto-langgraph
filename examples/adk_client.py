"""Call the Render LangGraph service from Google ADK.

  python examples/adk_client.py                       # connect and list the MCP tools (no LLM key needed)
  python examples/adk_client.py --ask "What are my PTO and STO balances?"   # needs GOOGLE_API_KEY

Environment:
  PTO_SERVICE_KEY   the service's SERVICE_KEY (the secret path segment). Required.
  PTO_BASE_URL      default https://pandalai-workday-pto.onrender.com
  GOOGLE_API_KEY    only for --ask (Gemini via ADK)
  ADK_MODEL         default gemini-2.0-flash
"""
import argparse
import asyncio
import os
import sys

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StreamableHTTPConnectionParams

BASE = os.environ.get("PTO_BASE_URL", "https://pandalai-workday-pto.onrender.com").rstrip("/")


def toolset() -> McpToolset:
    key = os.environ.get("PTO_SERVICE_KEY", "").strip()
    if not key:
        sys.exit("Set PTO_SERVICE_KEY (the service's SERVICE_KEY).")
    return McpToolset(connection_params=StreamableHTTPConnectionParams(url=f"{BASE}/mcp/{key}"))


async def list_tools() -> None:
    ts = toolset()
    try:
        tools = await ts.get_tools()
        print(f"Connected. {len(tools)} tool(s):")
        for t in tools:
            print(f" - {t.name}: {(t.description or '').strip().splitlines()[0][:100]}")
    finally:
        await ts.close()


async def ask(question: str) -> None:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    if not os.environ.get("GOOGLE_API_KEY"):
        sys.exit("--ask needs GOOGLE_API_KEY for the ADK (Gemini) model.")

    ts = toolset()
    agent = LlmAgent(
        name="hr_assistant",
        model=os.environ.get("ADK_MODEL", "gemini-2.0-flash"),
        instruction=(
            "For PTO, STO and time off questions, call ask_time_off with the user's question. "
            "Never set confirm_writes=true unless the user has explicitly agreed to a specific "
            "booking (date, absence type, amount); in that case repeat all details in the question."
        ),
        tools=[ts],
    )
    sessions = InMemorySessionService()
    runner = Runner(agent=agent, app_name="pto_demo", session_service=sessions)
    session = await sessions.create_session(app_name="pto_demo", user_id="demo")
    msg = types.Content(role="user", parts=[types.Part(text=question)])
    try:
        async for event in runner.run_async(user_id="demo", session_id=session.id, new_message=msg):
            if event.is_final_response() and event.content and event.content.parts:
                print(event.content.parts[0].text)
    finally:
        await ts.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask", metavar="QUESTION", help="run a full ADK agent with this question")
    args = ap.parse_args()
    try:
        asyncio.run(ask(args.ask) if args.ask else list_tools())
    except ConnectionError as e:
        sys.exit(f"Could not connect to {BASE}/mcp/<key>: {e}\n"
                 "A 'Not Found' usually means PTO_SERVICE_KEY does not match the service's SERVICE_KEY.")
