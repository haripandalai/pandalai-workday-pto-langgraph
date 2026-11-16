import asyncio, json, time
from urllib.parse import parse_qs, urlparse

import pytest

from app.agent import classify
from app.config import Settings
from app.workday_auth import NeedsLogin, TokenStore, WorkdayAuth, make_pkce


def S(**kw):
    base = dict(client_id="cid", client_secret="sec", public_base_url="https://svc.example.com")
    base.update(kw)
    return Settings(**base)


def test_classify_filters_to_absence_and_separates_writes():
    names = [
        "get_absence_balances", "list_time_off_entries", "request_time_off", "get_holiday_events",
        "find_payslips", "get_employee_compensation", "list_payroll_inputs", "create_expense_report",
        "get_current_user", "approve_business_process_event_step", "list_workers",
    ]
    out = classify(names, S())
    assert set(out["read"]) == {"get_absence_balances", "list_time_off_entries", "get_holiday_events", "get_current_user"}
    assert out["write"] == ["request_time_off"]
    for bad in ("find_payslips", "get_employee_compensation", "list_payroll_inputs", "create_expense_report", "list_workers"):
        assert bad in out["rejected"]


def test_pkce_challenge_matches_verifier():
    import base64, hashlib
    v, c = make_pkce()
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert 43 <= len(v) <= 128


def test_login_url_has_required_params(tmp_path):
    a = WorkdayAuth(S(token_path=str(tmp_path / "t.json")))
    a._meta = {"authorization_endpoint": "https://odp.agent.workday.com/auth/authorize/t", "token_endpoint": "x"}
    url = asyncio.run(a.login_url())
    q = parse_qs(urlparse(url).query)
    assert q["response_type"] == ["code"] and q["client_id"] == ["cid"]
    assert q["redirect_uri"] == ["https://svc.example.com/auth/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["state"][0] in a._pending


def test_exchange_rejects_unknown_state(tmp_path):
    a = WorkdayAuth(S(token_path=str(tmp_path / "t.json")))
    with pytest.raises(ValueError):
        asyncio.run(a.exchange_code("c", "nope"))


def test_token_store_roundtrip_and_permissions(tmp_path):
    p = tmp_path / "sub" / "t.json"
    st = TokenStore(str(p))
    assert st.load() is None
    st.save({"access_token": "a"})
    assert st.load() == {"access_token": "a"}
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_get_access_token_paths(tmp_path, monkeypatch):
    p = tmp_path / "t.json"
    a = WorkdayAuth(S(token_path=str(p)))
    a._meta = {"authorization_endpoint": "x", "token_endpoint": "https://tok"}
    with pytest.raises(NeedsLogin):
        asyncio.run(a.get_access_token())
    # fresh token is returned as-is
    a.store.save({"access_token": "fresh", "refresh_token": "r", "expires_at": time.time() + 3600})
    assert asyncio.run(a.get_access_token()) == "fresh"
    # expired, no refresh token -> NeedsLogin
    a.store.save({"access_token": "old", "refresh_token": None, "expires_at": time.time() - 5})
    with pytest.raises(NeedsLogin):
        asyncio.run(a.get_access_token())
    # expired with refresh token -> refreshes and keeps old refresh token if none returned
    a.store.save({"access_token": "old", "refresh_token": "r1", "expires_at": time.time() - 5})
    seen = {}

    async def fake(self, url, data, previous):
        seen.update(data)
        rec = {"access_token": "new", "refresh_token": previous["refresh_token"], "expires_at": time.time() + 3600, "obtained_at": 1}
        self.store.save(rec)
        return rec

    monkeypatch.setattr(WorkdayAuth, "_token_request", fake)
    assert asyncio.run(a.get_access_token()) == "new"
    assert seen["grant_type"] == "refresh_token" and seen["refresh_token"] == "r1"
    assert a.status()["connected"] is True


def test_system_prompt_includes_worker_only_when_configured():
    from app.agent import system_prompt
    assert "worker ID" not in system_prompt(S()).split("Rules:")[1].split("Be concise")[1]
    p = system_prompt(S(worker_name="Logan McNeil", worker_id="abc123"))
    assert "Logan McNeil" in p and "abc123" in p


def test_staffing_worker_search_stays_rejected():
    out = classify(["find_staffing_workers", "find_absence_balances"], S())
    assert out["rejected"] == ["find_staffing_workers"] and out["read"] == ["find_absence_balances"]


def test_service_key_with_slash_plus_equals_is_accepted():
    import app.server as srv
    object.__setattr__(srv.settings, "service_key", "ab/cd+ef=gh")
    assert srv._strip_key("/mcp/ab/cd+ef=gh") == ""
    assert srv._strip_key("/mcp/ab/cd+ef=gh/") == "/"
    assert srv._strip_key("/mcp/ab/cd+ef=gh/extra") == "/extra"
    assert srv._strip_key("/mcp/ab/cd+ef=gh-tail") is None   # key must end at a path boundary
    assert srv._strip_key("/mcp/ab/cd") is None
    assert srv._strip_key("/mcp/") is None
    object.__setattr__(srv.settings, "service_key", "ab/cd+ef=gh/")   # key that itself ends in '/'
    assert srv._strip_key("/mcp/ab/cd+ef=gh/") == ""
    object.__setattr__(srv.settings, "service_key", "")
    assert srv._strip_key("/mcp/") is None                    # empty key never matches
