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
