"""Visit planner: finds your next PTO day via A2A, then plans a local visit for it.

  python examples/a2a/planner.py --city "Boston, MA"
  python examples/a2a/planner.py --city "Austin, TX" --ask "I'm taking a PTO day soon. Plan a half-day visit."

Needs the PTO A2A server running (examples/a2a/pto_a2a_server.py) and GOOGLE_API_KEY.

  visit_planner (Gemini)
     |-- pto_agent   : RemoteA2aAgent -> A2A -> PTO agent -> MCP -> Render/LangGraph -> Workday
     '-- local_guide : Gemini + Google Search grounding for places near the city
"""
import argparse
import asyncio
import os
import sys

from google.adk.agents import LlmAgent
from google.adk.agents.remote_a2a_agent import AGENT_CARD_WELL_KNOWN_PATH, RemoteA2aAgent
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool

PTO_A2A_URL = os.environ.get("PTO_A2A_URL", "http://localhost:8001").rstrip("/")
MODEL = os.environ.get("ADK_MODEL", "gemini-3.8-flash")


def build_planner(city: str) -> LlmAgent:
    pto_agent = RemoteA2aAgent(
        name="pto_agent",
        description="Looks up the signed-in worker's upcoming PTO / time off dates and balances in Workday (read-only).",
        agent_card=f"{PTO_A2A_URL}{AGENT_CARD_WELL_KNOWN_PATH}",
    )
    local_guide = LlmAgent(
        name="local_guide",
        model=MODEL,
        description="Suggests specific places to visit near a city, using Google Search.",
        instruction=(
            "Suggest 3 specific places or activities near the given city for the given day, using Google Search. "
            "For each: name, what to do, rough travel time, and anything day-specific (weekday hours, season). "
            "Remind the user to confirm opening hours before going."
        ),
        tools=[google_search],
    )
    return LlmAgent(
        name="visit_planner",
        model=MODEL,
        instruction=(
            f"You plan a local day out for the user, who lives near {city}.\n"
            "1. Call pto_agent to find the user's next upcoming PTO day (date and weekday). "
            "If there is none, say so and stop; do not invent dates.\n"
            "2. Call local_guide with ONLY the city and that date/weekday (no balances, names or other "
            "Workday details), to get place ideas.\n"
            "3. Reply with the PTO date, then a short plan: pick one main place, a backup, and a simple "
            "morning/afternoon outline. Never book or change time off."
        ),
        tools=[AgentTool(agent=pto_agent), AgentTool(agent=local_guide)],
    )


async def run(city: str, question: str) -> None:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    if not os.environ.get("GOOGLE_API_KEY"):
        sys.exit("Set GOOGLE_API_KEY (Gemini).")
    sessions = InMemorySessionService()
    runner = Runner(agent=build_planner(city), app_name="visit_demo", session_service=sessions)
    session = await sessions.create_session(app_name="visit_demo", user_id="demo")
    msg = types.Content(role="user", parts=[types.Part(text=question)])
    async for event in runner.run_async(user_id="demo", session_id=session.id, new_message=msg):
        if event.is_final_response() and event.content and event.content.parts:
            print(event.content.parts[0].text)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True, help="where you live, e.g. 'Boston, MA'")
    ap.add_argument("--ask", default="Find my next PTO day and plan a relaxing local visit for it.")
    a = ap.parse_args()
    asyncio.run(run(a.city, a.ask))
