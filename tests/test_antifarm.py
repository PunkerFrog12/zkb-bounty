"""Fresh-alt / bounty-farming defences: hunter checks, 20% cap, 72 h hold, admin reject."""
import datetime as dt
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import func, select, update

from bounty import ledger
from bounty.config import Settings
from bounty.eve import EveClient
from bounty.models import Bounty, Claim, Deposit, User
from conftest import ALICE, BOB, HUNTER, HUNTER2, TARGET, fund, killmail


def active(sm, target=TARGET):
    with sm() as s:
        return s.scalar(select(Bounty).where(Bounty.target_character_id == target, Bounty.status == "ACTIVE"))


@pytest.fixture
def pool(sm, settings):
    fund(sm, settings, ALICE, 1_000_000_000)
    ledger.place_bounty(sm, settings, ALICE, TARGET, "Target", 100_000_000, False)
    return active(sm)


def test_production_defaults_are_anti_farming():
    s = Settings()
    assert (s.payout_ratio_percent, s.min_hunter_age_days, s.min_hunter_kills, s.claim_hold_hours) == (20, 90, 10, 72)


# ------------------------------------------------------------------ 20% cap

def test_ratio_20_makes_farming_a_losing_trade(sm, settings, pool):
    c = ledger.process_killmail(sm, replace(settings, payout_ratio_percent=20), killmail(1, value=100_000_000))
    # The target fed a 100M ship to their alt and got 19.5M back: an 80.5M loss.
    assert (c.gross_payout, c.net_payout) == (20_000_000, 19_500_000)
    assert active(sm).remaining_pool_isk == 80_000_000


# ------------------------------------------------------------------ hunter checks

ATTACKERS = [
    {"character_id": HUNTER, "corporation_id": 600, "damage_done": 50, "final_blow": True},
    {"character_id": HUNTER2, "corporation_id": 601, "damage_done": 900},
]


def test_fresh_alt_final_blow_falls_back_to_veteran(sm, settings, pool):
    fresh = {HUNTER}
    c = ledger.process_killmail(sm, settings, killmail(2, attackers=ATTACKERS),
                                hunter_ok=lambda cid, t: cid not in fresh)
    assert c.killer_character_id == HUNTER2


def test_only_fresh_alts_on_the_kill_pays_nothing(sm, settings, pool):
    assert ledger.process_killmail(sm, settings, killmail(3, attackers=ATTACKERS),
                                   hunter_ok=lambda cid, t: False) is None
    assert active(sm).remaining_pool_isk == 100_000_000


def test_hunter_check_stops_at_first_pass_and_skips_unbountied_kills(sm, settings, pool):
    calls = []

    def ok(cid, t):
        calls.append(cid)
        return True

    ledger.process_killmail(sm, settings, killmail(4, victim=999, attackers=ATTACKERS), hunter_ok=ok)
    assert calls == []  # no bounty on 999: no ESI / zKillboard calls at all
    ledger.process_killmail(sm, settings, killmail(5, attackers=ATTACKERS), hunter_ok=ok)
    assert calls == [HUNTER]  # final blow passed, nobody else looked up


def eve_with(birthday: str, kills):
    def handler(req: httpx.Request):
        if req.url.path.startswith("/characters/"):
            return httpx.Response(200, json={"name": "X", "birthday": birthday})
        if "/stats/characterID/" in req.url.path:
            return httpx.Response(200, json={} if kills is None else {"shipsDestroyed": kills})
        return httpx.Response(404)

    return EveClient(Settings(), http=httpx.Client(transport=httpx.MockTransport(handler)))


KILL_TIME = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("birthday,kills,ok", [
    ("2026-10-01T00:00:00Z", 500, False),  # 50 days old: fresh alt, however many kills
    ("2026-08-22T00:00:00Z", 10, True),    # exactly 90 days, exactly 10 kills
    ("2020-01-01T00:00:00Z", 9, False),    # old but no kill history
    ("2020-01-01T00:00:00Z", None, False),  # zKillboard has no stats: treated as 0 kills
    ("2020-01-01T00:00:00Z", 1234, True),
])
def test_eve_hunter_ok(birthday, kills, ok):
    assert eve_with(birthday, kills).hunter_ok(HUNTER, KILL_TIME) is ok


def test_age_is_measured_on_the_kill_date():
    # 100 days old today, but only 10 days old when the kill happened.
    eve = eve_with("2026-08-12T00:00:00Z", 50)
    assert eve.hunter_ok(HUNTER, dt.datetime(2026, 8, 22, tzinfo=dt.timezone.utc)) is False


# ------------------------------------------------------------------ 72 h hold

def test_claim_is_held_before_payment(sm, settings, pool):
    held = replace(settings, claim_hold_hours=72)
    c = ledger.process_killmail(sm, held, killmail(6))
    hours = (ledger._aware(c.payable_at) - ledger._aware(c.created_at)).total_seconds() / 3600
    assert 71.9 < hours < 72.1
    with pytest.raises(ledger.BountyError, match="on hold"):
        ledger.mark_paid(sm, c.id, 1)
    with sm.begin() as s:  # time passes
        s.execute(update(Claim).where(Claim.id == c.id).values(payable_at=ledger.utcnow() - dt.timedelta(minutes=1)))
    ledger.mark_paid(sm, c.id, 1)
    assert ledger.pending_payouts(sm) == []


# ------------------------------------------------------------------ admin reject

def test_reject_returns_isk_to_active_pool(sm, settings, pool):
    c = ledger.process_killmail(sm, settings, killmail(7, value=30_000_000))
    assert active(sm).remaining_pool_isk == 70_000_000
    ledger.reject_claim(sm, c.id, 1)
    assert active(sm).remaining_pool_isk == 100_000_000
    with sm() as s:
        assert s.get(Claim, c.id).status == "REJECTED"
    with pytest.raises(ledger.BountyError):
        ledger.reject_claim(sm, c.id, 1)  # only once
    with pytest.raises(ledger.BountyError):
        ledger.mark_paid(sm, c.id, 1)  # and never paid afterwards


def test_reject_reopens_a_drained_pool(sm, settings, pool):
    c = ledger.process_killmail(sm, settings, killmail(8))
    assert active(sm) is None
    ledger.reject_claim(sm, c.id, 1)
    b = active(sm)
    assert (b.id, b.remaining_pool_isk, b.total_pool_isk) == (pool.id, 100_000_000, 100_000_000)


def test_reject_after_new_pool_opened_joins_it(sm, settings, pool):
    c = ledger.process_killmail(sm, settings, killmail(9))
    fund(sm, settings, BOB, 50_000_000)
    ledger.place_bounty(sm, settings, BOB, TARGET, "Target", 50_000_000, False)
    ledger.reject_claim(sm, c.id, 1)
    b = active(sm)
    assert b.id != pool.id and (b.remaining_pool_isk, b.total_pool_isk) == (150_000_000, 150_000_000)


def test_paid_claim_cannot_be_rejected(sm, settings, pool):
    c = ledger.process_killmail(sm, settings, killmail(10))
    ledger.mark_paid(sm, c.id, 1)
    with pytest.raises(ledger.BountyError):
        ledger.reject_claim(sm, c.id, 1)


def test_isk_conserved_with_rejections(sm, settings, pool):
    a = ledger.process_killmail(sm, settings, killmail(11, value=30_000_000))
    ledger.process_killmail(sm, settings, killmail(12, value=20_000_000))
    ledger.reject_claim(sm, a.id, 1)
    with sm() as s:
        deposits = s.scalar(select(func.sum(Deposit.amount_isk)).where(Deposit.status == "CREDITED"))
        balances = s.scalar(select(func.sum(User.wallet_balance)))
        pools = s.scalar(select(func.sum(Bounty.remaining_pool_isk)))
        claimed = s.scalar(select(func.sum(Claim.gross_payout)).where(Claim.status != "REJECTED"))
    assert deposits == balances + pools + claimed
    assert pools == 80_000_000
