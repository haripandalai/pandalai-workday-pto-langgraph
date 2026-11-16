"""Environment-driven settings. Nothing secret is hard-coded."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Tools whose names match ALLOW (and not DENY) are exposed to the agent.
# Tune with GET /admin/tools?key=<ADMIN_KEY>, which lists every tool the Workday
# agent offers and whether it passes the filter.
DEFAULT_ALLOW = (
    r"absence|time_?off|timeoff|leave|holiday|\bpto\b|\bsto\b|_pto_|_sto_|"
    r"balance|current_user|whoami|get_me\b|my_worker|current_worker"
)
DEFAULT_DENY = r"payroll|compensation|salary|expense|supplier|requisition|recruit|prospect|goal|feedback"
# Names matching WRITE are only offered when the caller passes confirm_writes=true
# AND ENABLE_WRITE_TOOLS=true.
DEFAULT_WRITE = r"request|book|create|cancel|submit|delete|update|approve|correct|enter|add_|remove|rescind"


@dataclass(frozen=True)
class Settings:
    tenant: str = field(default_factory=lambda: _env("WORKDAY_TENANT", "wxqmdmpnjn_extdev"))
    agent_host: str = field(default_factory=lambda: _env("WORKDAY_AGENT_HOST", "https://odp.agent.workday.com"))
    client_id: str = field(default_factory=lambda: _env("WORKDAY_CLIENT_ID"))
    client_secret: str = field(default_factory=lambda: _env("WORKDAY_CLIENT_SECRET"))
    scope: str = field(default_factory=lambda: _env("WORKDAY_SCOPE"))

    public_base_url: str = field(
        default_factory=lambda: (_env("PUBLIC_BASE_URL") or _env("RENDER_EXTERNAL_URL") or "http://localhost:8000").rstrip("/")
    )
    service_key: str = field(default_factory=lambda: _env("SERVICE_KEY"))  # secret in claude.ai connector URL
    admin_key: str = field(default_factory=lambda: _env("ADMIN_KEY"))  # protects /auth/login and /admin/*

    token_path: str = field(default_factory=lambda: _env("TOKEN_STORE_PATH", "/data/workday_token.json"))

    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    model: str = field(default_factory=lambda: _env("MODEL", "claude-sonnet-5-5"))

    allow_regex: str = field(default_factory=lambda: _env("TOOL_ALLOW_REGEX", DEFAULT_ALLOW))
    deny_regex: str = field(default_factory=lambda: _env("TOOL_DENY_REGEX", DEFAULT_DENY))
    write_regex: str = field(default_factory=lambda: _env("TOOL_WRITE_REGEX", DEFAULT_WRITE))
    enable_write_tools: bool = field(default_factory=lambda: _env("ENABLE_WRITE_TOOLS", "false").lower() == "true")

    @property
    def mcp_url(self) -> str:
        return _env("WORKDAY_MCP_URL") or f"{self.agent_host}/{self.tenant}/mcp"

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url}/auth/callback"

    def allow(self) -> re.Pattern:
        return re.compile(self.allow_regex, re.I)

    def deny(self) -> re.Pattern:
        return re.compile(self.deny_regex, re.I)

    def write(self) -> re.Pattern:
        return re.compile(self.write_regex, re.I)


def get_settings() -> Settings:
    return Settings()
