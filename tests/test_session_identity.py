"""Tests for keeping the request identity consistent with the browser.

ModelScope's risk control sees the User-Agent, the anti-bot cookies and the
egress IP, not just `m_session_id`. These tests cover the pieces that keep
those signals aligned. Nothing here reaches the network.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.civision.accounts import save_jar, stored_jar
from app.civision.client import (
    CivisionClient,
    SessionInfo,
    probe_pasted_cookies,
    user_agent_for,
)
from app.models import Account, Base, Proxy


class TestUserAgent:
    def test_default_is_chrome(self):
        client = CivisionClient()
        assert "Chrome" in client.user_agent
        assert client._headers()["user-agent"] == client.user_agent

    def test_a_custom_user_agent_is_sent(self):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Firefox/135.0"
        client = CivisionClient(user_agent=ua)
        assert client.user_agent == ua
        assert client._headers()["user-agent"] == ua

    def test_browser_labels_resolve_to_a_user_agent(self):
        assert "Firefox" in user_agent_for("firefox")
        assert "Chrome" in user_agent_for("chrome")
        # An unknown label must not blow up; fall back to the default.
        assert "Chrome" in user_agent_for("netscape")

    def test_account_browser_label(self):
        assert Account(user_agent=None).browser == "chrome"
        assert Account(user_agent="Mozilla/5.0 Firefox/135.0").browser == "firefox"
        assert Account(user_agent="Mozilla/5.0 Chrome/131.0").browser == "chrome"


class TestCookiePersistence:
    def test_save_jar_keeps_every_cookie(self):
        account = Account()
        save_jar(
            account,
            {
                "m_session_id": "x",
                "h_uid": "1",
                "csrf_token": "t",
                "xsrf": "z",
                "cna": "c",
                "ssxmod_itna": "s1",
                "ssxmod_itna2": "s2",
                "tfstk": "t",
                "isg": "i",
                "_ga": "analytics",
                "modelscope-theme": "dark",
            },
        )
        jar = stored_jar(account)
        assert {
            "m_session_id",
            "h_uid",
            "csrf_token",
            "xsrf",
            "cna",
            "ssxmod_itna",
            "ssxmod_itna2",
            "tfstk",
            "isg",
            "_ga",
            "modelscope-theme",
        } <= set(jar)

    async def test_every_set_cookie_is_kept(self, monkeypatch):
        """Upstream may rotate m_session_id; losing the new value logs it out."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers=[
                    ("set-cookie", "m_session_id=new-session; Path=/; HttpOnly"),
                    ("set-cookie", "h_uid=42; Path=/; HttpOnly"),
                    ("set-cookie", "cna=abc; Path=/"),
                    ("set-cookie", "isg=def; Path=/"),
                    ("set-cookie", "csrf_token=fresh; Path=/; Max-Age=2592000"),
                ],
                json={"Code": 200, "Data": {}},
            )

        transport = httpx.MockTransport(handler)

        def patched(self):
            return httpx.AsyncClient(
                headers=self._headers(),
                cookies=self.jar,
                timeout=10,
                follow_redirects=False,
                transport=transport,
            )

        monkeypatch.setattr(CivisionClient, "_client", patched)
        client = CivisionClient(
            jar={"m_session_id": "old", "h_uid": "1", "csrf_token": "old"}
        )
        await client.whoami()

        assert client.jar["m_session_id"] == "new-session"
        assert client.jar["h_uid"] == "42"
        assert client.jar["cna"] == "abc"
        assert client.jar["isg"] == "def"
        assert client.jar["csrf_token"] == "fresh"


class TestProbeIdentity:
    async def test_probe_uses_the_import_user_agent(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_check(self):
            seen["ua"] = self.user_agent
            return SessionInfo(authenticated=True)

        monkeypatch.setattr(CivisionClient, "check_session", fake_check)

        info = await probe_pasted_cookies(
            "m_session_id=x; h_uid=y",
            user_agent="Mozilla/5.0 Firefox/135.0",
        )
        assert info.authenticated
        assert seen["ua"] == "Mozilla/5.0 Firefox/135.0"

    async def test_environment_proxies_are_not_used_by_default(self, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        client = CivisionClient()._client()
        try:
            assert client.trust_env is False
        finally:
            await client.aclose()


@pytest.fixture
async def db_session(tmp_path):
    """A private database, so these tests do not touch the shared one."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'session-identity.db').as_posix()}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


class TestAccountImport:
    async def test_the_import_user_agent_is_stored(self, monkeypatch, db_session):
        from app.civision import accounts as accounts_mod

        async def fake_probe(raw, proxy=None, user_agent=None):
            assert user_agent == "Mozilla/5.0 Firefox/135.0"
            return SessionInfo(
                authenticated=True,
                cookie_jar={"m_session_id": "x", "h_uid": "1"},
            )

        async def fake_refresh(session, account, proxy=None):
            return {"total": 0.0, "available": 0.0, "frozen": 0.0}

        monkeypatch.setattr(accounts_mod, "probe_pasted_cookies", fake_probe)
        monkeypatch.setattr(accounts_mod, "refresh_balance", fake_refresh)

        account, info = await accounts_mod.add_account_from_cookies(
            db_session,
            name="camoufox",
            cookie_text="m_session_id=x; h_uid=1",
            user_agent="Mozilla/5.0 Firefox/135.0",
        )

        assert info.authenticated
        assert account is not None
        assert account.user_agent == "Mozilla/5.0 Firefox/135.0"


class TestValidateAccount:
    async def test_a_transient_probe_does_not_expire(self, monkeypatch, db_session):
        from app.civision import accounts as accounts_mod

        class FakeClient:
            async def check_session(self):
                return SessionInfo(
                    authenticated=False,
                    transient=True,
                    reason="upstream rate limited the probe",
                )

        monkeypatch.setattr(
            accounts_mod,
            "client_for",
            lambda account, proxy=None: FakeClient(),
        )
        account = Account(name="a-transient", status="active")
        db_session.add(account)
        await db_session.flush()

        info = await accounts_mod.validate_account(db_session, account)

        assert info.transient is True
        assert account.status == "active"
        assert account.last_error == "upstream rate limited the probe"
        assert account.last_validated_at is not None

    async def test_a_confirmed_rejection_expires(self, monkeypatch, db_session):
        from app.civision import accounts as accounts_mod

        class FakeClient:
            async def check_session(self):
                return SessionInfo(
                    authenticated=False,
                    reason="ModelScope rejected the session",
                )

        monkeypatch.setattr(
            accounts_mod,
            "client_for",
            lambda account, proxy=None: FakeClient(),
        )
        account = Account(name="a-rejected", status="active")
        db_session.add(account)
        await db_session.flush()

        info = await accounts_mod.validate_account(db_session, account)

        assert info.transient is False
        assert account.status == "expired"
        assert account.last_error == "ModelScope rejected the session"


class TestMaintenanceClassification:
    async def test_transient_results_do_not_expire(self, monkeypatch, db_session):
        from contextlib import asynccontextmanager

        from app.services import maintenance as maint_mod

        @asynccontextmanager
        async def fake_scope():
            yield db_session

        async def fake_validate(session, account, proxy=None):
            account.last_error = "upstream blocked the probe"
            return SessionInfo(
                authenticated=False,
                transient=True,
                reason="upstream blocked the probe",
            )

        monkeypatch.setattr(maint_mod, "session_scope", fake_scope)
        monkeypatch.setattr(maint_mod, "validate_account", fake_validate)

        account = Account(name="a-maint", status="active")
        db_session.add(account)
        await db_session.flush()

        summary = await maint_mod.maintenance.check_sessions()

        assert summary["transient"] == 1
        assert summary["expired"] == 0
        assert account.status == "active"


class TestProxyAssignment:
    async def test_auto_assignment_is_off_by_default(self, monkeypatch, db_session):
        from app.civision import accounts as accounts_mod

        class Settings:
            auto_assign_proxy = False

        monkeypatch.setattr(accounts_mod, "get_settings", lambda: Settings())

        proxy = Proxy(name="p1", url="http://127.0.0.1:1", enabled=True, healthy=True)
        account = Account(name="a1", status="active")
        db_session.add_all([proxy, account])
        await db_session.flush()

        chosen = await accounts_mod.assign_proxy(db_session, account)
        assert chosen is None
        assert account.proxy_id is None
        assert account.proxy_auto_assigned is False

    async def test_auto_assignment_marks_the_proxy_as_automatic(
        self, monkeypatch, db_session
    ):
        from app.civision import accounts as accounts_mod

        class Settings:
            auto_assign_proxy = True

        monkeypatch.setattr(accounts_mod, "get_settings", lambda: Settings())

        proxy = Proxy(name="p1", url="http://127.0.0.1:1", enabled=True, healthy=True)
        account = Account(name="a1", status="active")
        db_session.add_all([proxy, account])
        await db_session.flush()

        chosen = await accounts_mod.assign_proxy(db_session, account)
        assert chosen is not None
        assert account.proxy_id == chosen.id
        assert account.proxy_auto_assigned is True

    async def test_an_unhealthy_manual_proxy_is_left_for_the_operator(
        self, monkeypatch, db_session
    ):
        from app.civision import accounts as accounts_mod

        class Settings:
            auto_assign_proxy = False

        monkeypatch.setattr(accounts_mod, "get_settings", lambda: Settings())

        proxy = Proxy(name="p1", url="http://127.0.0.1:1", enabled=True, healthy=False)
        account = Account(name="a1", status="active")
        db_session.add_all([proxy, account])
        await db_session.flush()
        account.proxy_id = proxy.id
        await db_session.flush()

        chosen = await accounts_mod.assign_proxy(db_session, account)
        assert chosen is None
        assert account.proxy_id == proxy.id
        assert account.proxy_auto_assigned is False

    async def test_an_unhealthy_auto_proxy_is_cleared(
        self, monkeypatch, db_session
    ):
        from app.civision import accounts as accounts_mod

        class Settings:
            auto_assign_proxy = False

        monkeypatch.setattr(accounts_mod, "get_settings", lambda: Settings())

        proxy = Proxy(name="p1", url="http://127.0.0.1:1", enabled=True, healthy=False)
        account = Account(name="a1", status="active")
        db_session.add_all([proxy, account])
        await db_session.flush()
        account.proxy_id = proxy.id
        account.proxy_auto_assigned = True
        await db_session.flush()

        chosen = await accounts_mod.assign_proxy(db_session, account)
        assert chosen is None
        assert account.proxy_id is None
        assert account.proxy_auto_assigned is False

    async def test_an_unhealthy_auto_proxy_is_replaced_when_auto_is_on(
        self, monkeypatch, db_session
    ):
        from app.civision import accounts as accounts_mod

        class Settings:
            auto_assign_proxy = True

        monkeypatch.setattr(accounts_mod, "get_settings", lambda: Settings())

        old = Proxy(name="old", url="http://127.0.0.1:1", enabled=True, healthy=False)
        new = Proxy(name="new", url="http://127.0.0.1:2", enabled=True, healthy=True)
        account = Account(name="a1", status="active")
        db_session.add_all([old, new, account])
        await db_session.flush()
        account.proxy_id = old.id
        account.proxy_auto_assigned = True
        await db_session.flush()

        chosen = await accounts_mod.assign_proxy(db_session, account)
        assert chosen is not None
        assert chosen.id == new.id
        assert account.proxy_id == new.id
        assert account.proxy_auto_assigned is True


class TestRunnerCookiePersistence:
    async def test_finish_account_job_saves_rotated_cookies(self, db_session):
        from app.civision.client import CivisionClient
        from app.services.runner import _finish_account_job

        account = Account(name="a-runner", status="active", running_jobs=1)
        db_session.add(account)
        await db_session.flush()

        client = CivisionClient(
            jar={"m_session_id": "new-session", "csrf_token": "fresh"}
        )
        await _finish_account_job(db_session, account.id, client)

        assert account.running_jobs == 0
        jar = stored_jar(account)
        assert jar["m_session_id"] == "new-session"
        assert jar["csrf_token"] == "fresh"


class TestAccountRowTemplate:
    def test_renders_proxy_and_browser_controls(self):
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader("app/templates"))
        template = env.get_template("_account_row.html")
        account = Account(
            id=1,
            name="acct",
            status="active",
            auth_source="cookie",
            user_agent="Mozilla/5.0 Firefox/135.0",
            proxy_auto_assigned=True,
            available_balance=0.0,
            running_jobs=0,
            max_concurrency=1,
        )
        account.proxy = Proxy(id=2, name="p1")

        html = template.render(a=account, proxies=[account.proxy])

        assert 'action="/admin/accounts/1/proxy"' in html
        assert 'action="/admin/accounts/1/browser"' in html
        assert "Firefox" in html
        assert "p1" in html
