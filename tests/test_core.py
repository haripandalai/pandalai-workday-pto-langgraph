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


# ---- Path 2: Workday-hosted agent over A2A ---------------------------------------------
from app import a2a_agent


def test_a2a_urls_and_second_client_login(tmp_path):
    s = S(pto_agent_wid="abc123", a2a_client_id="bridge-id", a2a_client_secret="bridge-sec")
    assert s.a2a_url == "https://odp.agent.workday.com/v1/a2a/wxqmdmpnjn_extdev/abc123"
    assert s.a2a_redirect_uri == "https://svc.example.com/auth/a2a/callback"
    a = WorkdayAuth(
        s, TokenStore(str(tmp_path / "a2a.json")),
        client_id=s.a2a_client_id, client_secret=s.a2a_client_secret, redirect_uri=s.a2a_redirect_uri,
    )
    a._meta = {"authorization_endpoint": "https://odp.agent.workday.com/auth/authorize/t", "token_endpoint": "x"}
    q = parse_qs(urlparse(asyncio.run(a.login_url())).query)
    assert q["client_id"] == ["bridge-id"]
    assert q["redirect_uri"] == ["https://svc.example.com/auth/a2a/callback"]
    assert q["code_challenge_method"] == ["S256"]
    # the default client is untouched
    d = WorkdayAuth(S(token_path=str(tmp_path / "d.json")))
    d._meta = a._meta
    assert parse_qs(urlparse(asyncio.run(d.login_url())).query)["client_id"] == ["cid"]


def test_a2a_parse_result_variants():
    ok = {"result": {"contextId": "ctx", "status": {"state": "completed"},
                     "artifacts": [{"parts": [{"kind": "text", "text": "PTO 200h"}]}]}}
    assert a2a_agent.parse_result(ok) == ("completed", "PTO 200h", "ctx")
    hist = {"result": {"status": {"state": "completed"}, "history": [
        {"role": "user", "parts": [{"kind": "text", "text": "q"}]},
        {"role": "agent", "parts": [{"kind": "text", "text": "from history"}]}]}}
    assert a2a_agent.parse_result(hist)[1] == "from history"
    err = {"error": {"code": -32602, "message": "Invalid or unsupported credentials"}}
    state, text, _ = a2a_agent.parse_result(err)
    assert state == "error" and "Invalid or unsupported credentials" in text


def test_a2a_ask_blocks_write_intent_locally_and_sends_plain_text(tmp_path, monkeypatch):
    s = S(pto_agent_wid="w", enable_write_tools=True)
    seen = {"posts": 0}

    class R:
        status_code = 200
        def json(self):
            return {"result": {"status": {"state": "completed"}, "contextId": "c1",
                               "artifacts": [{"parts": [{"kind": "text", "text": "ok"}]}]}}

    class C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            seen["posts"] += 1
            seen["text"] = json["params"]["message"]["parts"][0]["text"]
            seen["auth"] = headers["Authorization"]
            return R()

    monkeypatch.setattr(a2a_agent.httpx, "AsyncClient", C)
    store = TokenStore(str(tmp_path / "t.json"))
    auth = WorkdayAuth(s, store)
    assert "not connected" in asyncio.run(a2a_agent.ask(s, auth, "balances?"))
    store.save({"access_token": "tok", "refresh_token": "r", "expires_at": time.time() + 3600, "obtained_at": 0})
    # plain read goes through unchanged, with no prefix
    out = asyncio.run(a2a_agent.ask(s, auth, "List my time off requests for 2027"))
    assert out.startswith("ok") and "[context_id: c1]" in out
    assert seen["text"] == "List my time off requests for 2027" and seen["auth"] == "Bearer tok"
    # a booking verb without confirm_writes is refused locally, nothing is posted
    n = seen["posts"]
    out = asyncio.run(a2a_agent.ask(s, auth, "Book 8 hours of vacation on 2027-02-11"))
    assert out.startswith("Not sent to Workday") and seen["posts"] == n
    # with confirm_writes on a service that allows writes, it is sent as written
    asyncio.run(a2a_agent.ask(s, auth, "Yes, go ahead and submit the request: Vacation, 8 hours, 2027-02-11.", confirm_writes=True))
    assert seen["posts"] == n + 1 and seen["text"].startswith("Yes, go ahead")
    # writes disabled on the service: refused even with confirm_writes
    s2 = S(pto_agent_wid="w", enable_write_tools=False)
    out = asyncio.run(a2a_agent.ask(s2, WorkdayAuth(s2, store), "submit it", confirm_writes=True))
    assert "disabled on this service" in out and seen["posts"] == n + 1
