import re

import pytest
from fastapi.testclient import TestClient

from bounty import ledger
from bounty.models import KV
from bounty.web import create_app, parse_isk
from bounty.workers import WALLET_REFRESH_TOKEN, killfeed_step
from conftest import ADMIN, ALICE, BOB, RECEIVER, TARGET, fund, killmail

NAMES = {ALICE: "Alice", BOB: "Bob", TARGET: "Evil Target", ADMIN: "Admin", RECEIVER: "zKillboard"}


class FakeEve:
    """Stands in for EVE SSO/ESI/R2Z2. The 'code' passed to /callback is the character id."""

    def __init__(self):
        self.scopes = []
        self.feed = {}

    def authorize_url(self, redirect_uri, scopes):
        self.scopes = scopes
        return "https://login.example/authorize", "state123"

    def exchange_code(self, code):
        return {"access_token": code, "refresh_token": f"rt-{code}"}

    def verify_access_token(self, tok):
        cid = int(tok)
        return {"character_id": cid, "name": NAMES.get(cid, f"Pilot {cid}"), "scopes": self.scopes}

    def character_name(self, cid):
        return NAMES.get(cid)

    def resolve_character(self, name):
        return next(((k, v) for k, v in NAMES.items() if v.lower() == name.lower()), None)

    def verify_kill(self, d):
        return True

    def hunter_ok(self, cid, kill_time):
        return True

    def r2z2_sequence(self):
        return 1

    def r2z2_package(self, seq):
        return self.feed.get(seq)


@pytest.fixture
def eve():
    return FakeEve()


@pytest.fixture
def client(sm, settings, eve):
    return TestClient(create_app(settings, sm, eve), base_url="https://testserver")


def login(client, cid, wallet=False):
    client.get("/login" + ("?wallet=1" if wallet else ""), follow_redirects=False)
    return client.get(f"/callback?code={cid}&state=state123", follow_redirects=False)


def csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


@pytest.mark.parametrize("raw,isk", [("5000000", 5_000_000), ("5,000,000", 5_000_000), ("5m", 5_000_000),
                                     ("1.5b", 1_500_000_000), ("250k", 250_000), ("12.34m ISK", 12_340_000)])
def test_parse_isk(raw, isk):
    assert parse_isk(raw) == isk


def test_bad_oauth_state_rejected(client):
    client.get("/login", follow_redirects=False)
    assert client.get("/callback?code=1001&state=evil").status_code == 400


def test_full_flow_place_anonymous_bounty(client, sm, settings):
    login(client, ALICE)
    with sm() as s:
        from bounty.models import User
        code = s.get(User, ALICE).deposit_code
    assert code in client.get("/account").text  # deposit instructions show the code
    fund(sm, settings, ALICE, 100_000_000)
    token = csrf(client, f"/character/{TARGET}")
    r = client.post(f"/character/{TARGET}/bounty", data={"amount": "4m", "csrf": token})
    assert "minimum bounty is 5,000,000 ISK" in r.text  # flash shown after redirect
    r = client.post(f"/character/{TARGET}/bounty", data={"amount": "60m", "anonymous": "1", "csrf": token})
    assert r.status_code == 200
    page = client.get(f"/character/{TARGET}").text
    assert "60,000,000 ISK" in page and "Anonymous" in page and "Alice" not in page.split("Contributors")[1]
    api = client.get(f"/api/bounty/{TARGET}").json()
    assert api["remaining_pool_isk"] == 60_000_000 and api["contributors"] == 1
    assert str(ALICE) not in str(api)


def test_post_requires_csrf(client, sm, settings):
    login(client, ALICE)
    fund(sm, settings, ALICE, 100_000_000)
    assert client.post(f"/character/{TARGET}/bounty", data={"amount": "10m"}).status_code == 400


def test_admin_queue_is_admin_only(client, sm, settings):
    login(client, ALICE)
    assert client.get("/admin").status_code == 403
    login(client, ADMIN)
    assert client.get("/admin").status_code == 200


def test_admin_marks_claim_paid(client, sm, settings):
    fund(sm, settings, ALICE, 100_000_000)
    ledger.place_bounty(sm, settings, ALICE, TARGET, "Evil Target", 100_000_000, False)
    c = ledger.process_killmail(sm, settings, killmail(1))
    login(client, ADMIN)
    page = client.get("/admin").text
    assert f"CLAIM-{c.id}" in page and "97,500,000" in page
    client.post(f"/admin/claims/{c.id}/paid", data={"csrf": csrf(client, "/admin")})
    assert ledger.pending_payouts(sm) == []


def test_admin_sees_hold_and_can_reject(sm, settings, eve):
    from dataclasses import replace
    held = replace(settings, claim_hold_hours=72)
    client = TestClient(create_app(held, sm, eve), base_url="https://testserver")
    fund(sm, held, ALICE, 100_000_000)
    ledger.place_bounty(sm, held, ALICE, TARGET, "Evil Target", 100_000_000, False)
    c = ledger.process_killmail(sm, held, killmail(2))
    login(client, ADMIN)
    page = client.get("/admin").text
    assert "On hold until" in page and f"/admin/claims/{c.id}/paid" not in page
    r = client.post(f"/admin/claims/{c.id}/reject", data={"csrf": csrf(client, "/admin")})
    assert "went back to the pool" in r.text
    assert client.get(f"/api/bounty/{TARGET}").json()["remaining_pool_isk"] == 100_000_000


def test_receiving_character_login_stores_wallet_token(client, sm):
    login(client, RECEIVER, wallet=True)
    with sm() as s:
        assert s.get(KV, WALLET_REFRESH_TOKEN).value == f"rt-{RECEIVER}"


def test_search_by_name(client):
    r = client.get("/search?q=evil target", follow_redirects=False)
    assert r.headers["location"] == f"/character/{TARGET}"


def test_killfeed_step_advances_cursor_and_claims(sm, settings, eve):
    fund(sm, settings, ALICE, 100_000_000)
    ledger.place_bounty(sm, settings, ALICE, TARGET, "Evil Target", 50_000_000, False)
    eve.feed = {1: killmail(77, value=20_000_000), 2: killmail(77, value=20_000_000)}  # re-sent kill
    assert killfeed_step(sm, settings, eve) is True
    assert killfeed_step(sm, settings, eve) is True
    assert killfeed_step(sm, settings, eve) is False  # 404 -> caller sleeps 6s
    assert [c.killmail_id for c in ledger.pending_payouts(sm)] == [77]
