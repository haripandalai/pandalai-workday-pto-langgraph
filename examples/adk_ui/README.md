# ADK dev UI for the PTO service

Start and stop from any terminal (no Claude session needed). One-time: save the `SERVICE_KEY` from Render
(service > Environment) with `umask 077; pbpaste | tr -d '[:space:]' > ~/.pto_service_key`, and keep your Gemini key in `~/.gemini_key`.

```sh
scripts/adk-ui.sh start     # first run builds .venv-adk; serves http://localhost:8090 (localhost only)
scripts/adk-ui.sh status
scripts/adk-ui.sh logs
scripts/adk-ui.sh stop
```

Manual equivalent:

```sh
python -m venv .venv-adk && . .venv-adk/bin/activate
pip install -r ../requirements.txt
export PTO_SERVICE_KEY=$(cat ~/.pto_service_key)   # the service's SERVICE_KEY
export GOOGLE_API_KEY=$(cat ~/.gemini_key)
cd examples/adk_ui && adk web --port 8090 .
```
Open http://localhost:8090, pick **pto_assistant**, and ask e.g. "List my time off between 2026-01-01 and 2027-12-31"
or "Compare both tools on my PTO balances". The chat is read-only (`confirm_writes` is forced off).

## Questions to try (all read-only)

**Balances**
- What are my PTO and Floating Holiday balances?
- How many hours of PTO will I have left if I take 5 days off?

**Your time off**
- List my time off between 2026-01-01 and 2027-12-31.
- Do I have any upcoming time off?
- Show only my approved vacation.
- What's the status of my time off on 2027-02-09?

**What you can book**
- Which time-off types am I eligible for?
- Which dates are valid for time off in February 2027?
- Is 2027-02-11 a valid day to take off?

**Holidays**
- What are the company holidays in 2027?
- Which holidays fall on a weekday in March?

**Reference**
- What do the different time-off entry statuses mean?
- What leave event statuses exist?

**Using both tools**
- Compare both tools on my PTO balance. (`ask_time_off` is our LangGraph agent; `ask_workday_agent` is Workday's own.)
- Follow-ups such as "and for Floating Holiday?" work on the Workday-hosted agent, which keeps the chat via `context_id`.

**Won't work here**
- Booking or cancelling: refused locally in this read-only UI. Book in Agent Chat with `pto-agent`, where you approve it.
- Anything outside time off (pay, org charts, other workers): those tools are left out on purpose.
