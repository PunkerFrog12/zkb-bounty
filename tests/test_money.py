import random
import threading
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from bounty import ledger
from bounty.models import Bounty, BountyContribution, Claim, Deposit, User
from conftest import ALICE, BOB, CAROL, HUNTER, HUNTER2, RECEIVER, TARGET, fund, journal, killmail


def balance(sm, cid):
    with sm() as s:
        return s.get(User, cid).wallet_balance


def active(sm, target=TARGET):
    with sm() as s:
        return s.scalar(select(Bounty).where(Bounty.target_character_id == target, Bounty.status == "ACTIVE"))


def all_claims(sm):
    with sm() as s:
        return list(s.scalars(select(Claim).order_by(Claim.id)))


# ------------------------------------------------------------------ fee math

@pytest.mark.parametrize("gross,fee,net", [
    (100_000_000, 2_500_000, 97_500_000),
    (5_000_000, 125_000, 4_875_000),
    (1_000_000_000_000, 25_000_000_000, 975_000_000_000),  # 1T ISK, exact integer math
    (20, 1, 19),   # 0.5 ISK fee rounds half-up
    (19, 0, 19),   # 0.475 rounds down
    (0, 0, 0),
])
def test_split_fee(gross, fee, net):
    assert ledger.split_fee(gross, 25) == (fee, net)


def test_split_fee_always_sums_to_gross():
    rng = random.Random(42)
    for _ in range(10_000):
        g = rng.randrange(0, 10**15)
        fee, net = ledger.split_fee(g, 25)
        assert fee + net == g and 0 <= fee <= g


def test_gross_capped_by_pool_and_ratio():
    assert ledger.gross_for_kill(50_000_000, 1_000_000_000, 100) == 50_000_000  # pool caps
    assert ledger.gross_for_kill(50_000_000, 10_000_000, 100) == 10_000_000     # ISK destroyed caps
    assert ledger.gross_for_kill(50_000_000, 10_000_000, 20) == 2_000_000       # CCP-style 20%


# ------------------------------------------------------------------ minimums & placing

def test_minimum_bounty_enforced(sm, settings):
    fund(sm, settings, ALICE, 20_000_000)
    with pytest.raises(ledger.BountyError, match="minimum"):
        ledger.place_bounty(sm, settings, ALICE, TARGET, "Target", 4_999_999, False)
    assert balance(sm, ALICE) == 20_000_000
    ledger.place_bounty(sm, settings, ALICE, TARGET, "Target", 5_000_000, False)
    assert balance(sm, ALICE) == 15_000_000


def test_no_maximum(sm, settings):
    fund(sm, settings, ALICE, 500_000_000_000_000)  # 500T ISK
    ledger.place_bounty(sm, settings, ALICE, TARGET, "Target", 500_000_000_000_000, False)
    assert active(sm).remaining_pool_isk == 500_000_000_000_000


@pytest.mark.parametrize("amount", [5_000_000.5, "5000000", True])
def test_amount_must_be_whole_isk_int(sm, settings, amount):
    fund(sm, settings, ALICE, 20_000_000)
    with pytest.raises(ledger.BountyError):
        ledger.place_bounty(sm, settings, ALICE, TARGET, "Target", amount, False)


def test_insufficient_balance_changes_nothing(sm, settings):
    fund(sm, settings, ALICE, 9_000_000)
    with pytest.raises(ledger.BountyError, match="insufficient"):
        ledger.place_bounty(sm, settings, ALICE, TARGET, "Target", 10_000_000, False)
    assert balance(sm, ALICE) == 9_000_000
    assert active(sm) is None


def test_unknown_user_cannot_place(sm, settings):
    with pytest.raises(ledger.BountyError, match="insufficient"):
        ledger.place_bounty(sm, settings, 424242, TARGET, "Target", 10_000_000, False)


def test_cannot_bounty_yourself(sm, settings):
    fund(sm, settings, ALICE, 20_000_000)
    with pytest.raises(ledger.BountyError, match="yourself"):
        ledger.place_bounty(sm, settings, ALICE, ALICE, "Alice", 10_000_000, False)


def test_contributions_pool_together_with_anonymity(sm, settings):
    fund(sm, settings, ALICE, 100_000_000)
    fund(sm, settings, BOB, 100_000_000)
    ledger.place_bounty(sm, settings, ALICE, TARGET, "Target", 10_000_000, False)
    ledger.place_bounty(sm, settings, BOB, TARGET, "Target", 30_000_000, True)
    b = active(sm)
    assert (b.total_pool_isk, b.remaining_pool_isk) == (40_000_000, 40_000_000)
    with sm() as s:
        rows = list(s.scalars(select(BountyContribution).order_by(BountyContribution.id)))
        assert [(r.placer_character_id, r.anonymous, r.bounty_id) for r in rows] == \
            [(ALICE, False, b.id), (BOB, True, b.id)]
        assert s.scalar(select(func.count(Bounty.id))) == 1


# ------------------------------------------------------------------ deposits

def test_deposit_by_code_is_idempotent(sm, settings):
    u = ledger.get_or_create_user(sm, settings, ALICE, "Alice")
    e = journal(1, 50_000_000, f"for bounties {u.deposit_code.lower()} thx")
    r1 = ledger.record_journal_entries(sm, settings, [e])
    r2 = ledger.record_journal_entries(sm, settings, [e, e])
    assert r1["credited"] == 1 and r2["credited"] == 0
    assert balance(sm, ALICE) == 50_000_000


def test_deposit_without_code_is_unmatched_then_assignable(sm, settings):
    ledger.get_or_create_user(sm, settings, ALICE, "Alice")
    ledger.record_journal_entries(sm, settings, [journal(2, 7_000_000, "hi zkill")])
    assert balance(sm, ALICE) == 0
    with sm() as s:
        assert s.get(Deposit, 2).status == "UNMATCHED"
    ledger.assign_unmatched_deposit(sm, 2, ALICE)
    assert balance(sm, ALICE) == 7_000_000
    with pytest.raises(ledger.BountyError):
        ledger.assign_unmatched_deposit(sm, 2, ALICE)  # cannot credit twice
    assert balance(sm, ALICE) == 7_000_000


def test_deposit_by_sender_only_when_enabled(sm, settings):
    ledger.get_or_create_user(sm, settings, ALICE, "Alice")
    ledger.record_journal_entries(sm, replace(settings, match_deposits_by_sender=True), [journal(3, 6_000_000)])
    assert balance(sm, ALICE) == 6_000_000


def test_deposit_filters(sm, settings):
    u = ledger.get_or_create_user(sm, settings, ALICE, "Alice")
    res = ledger.record_journal_entries(sm, settings, [
        journal(4, 9_000_000, u.deposit_code, receiver=777),           # someone else's wallet
        journal(5, 9_000_000, u.deposit_code, ref_type="bounty_prizes"),  # not a donation
        journal(6, 0, u.deposit_code),
    ])
    assert res["credited"] == 0 and balance(sm, ALICE) == 0


def test_cents_are_floored(sm, settings):
    u = ledger.get_or_create_user(sm, settings, ALICE, "Alice")
    ledger.record_journal_entries(sm, settings, [journal(7, 5_000_000.99, u.deposit_code)])
    assert balance(sm, ALICE) == 5_000_000


# ------------------------------------------------------------------ claims

@pytest.fixture
def pool(sm, settings):
    fund(sm, settings, ALICE, 1_000_000_000)
    ledger.place_bounty(sm, settings, ALICE, TARGET, "Target", 100_000_000, False)
    return active(sm)


def test_kill_pays_final_blow_minus_fee(sm, settings, pool):
    c = ledger.process_killmail(sm, settings, killmail(1, attackers=[
        {"character_id": HUNTER2, "corporation_id": 600, "damage_done": 9000, "final_blow": False},
        {"character_id": HUNTER, "corporation_id": 600, "damage_done": 10, "final_blow": True},
    ]))
    assert (c.killer_character_id, c.gross_payout, c.host_fee_isk, c.net_payout, c.status) == \
        (HUNTER, 100_000_000, 2_500_000, 97_500_000, "PENDING_PAYMENT")
    with sm() as s:
        b = s.get(Bounty, pool.id)
        assert (b.remaining_pool_isk, b.status) == (0, "DEPLETED")


def test_npc_final_blow_falls_back_to_top_damage_player(sm, settings, pool):
    c = ledger.process_killmail(sm, settings, killmail(2, attackers=[
        {"corporation_id": 1000125, "damage_done": 5000, "final_blow": True},  # NPC, no character_id
        {"character_id": HUNTER, "corporation_id": 600, "damage_done": 10},
        {"character_id": HUNTER2, "corporation_id": 600, "damage_done": 900},
    ]))
    assert c.killer_character_id == HUNTER2


def test_partial_payout_capped_by_isk_destroyed(sm, settings, pool):
    c = ledger.process_killmail(sm, settings, killmail(3, value=30_000_000))
    assert c.gross_payout == 30_000_000
    assert active(sm).remaining_pool_isk == 70_000_000


def test_same_killmail_never_pays_twice(sm, settings, pool):
    km = killmail(4, value=10_000_000)
    assert ledger.process_killmail(sm, settings, km) is not None
    assert ledger.process_killmail(sm, settings, km) is None  # R2Z2 re-sends re-processed kills
    assert len(all_claims(sm)) == 1
    assert active(sm).remaining_pool_isk == 90_000_000


@pytest.mark.parametrize("kw", [
    {"npc": True}, {"awox": True},
    {"attackers": [{"character_id": HUNTER, "corporation_id": 500, "final_blow": True}]},  # same corp
    {"victim_alliance": 99, "attackers": [{"character_id": HUNTER, "corporation_id": 7, "alliance_id": 99,
                                          "final_blow": True}]},  # same alliance
    {"attackers": [{"character_id": TARGET, "corporation_id": 500, "final_blow": True}]},  # self
    {"victim": CAROL},  # not the target
    {"time": "2000-01-01T00:00:00Z"},  # kill predates the bounty
    {"value": 0},
])
def test_ineligible_kills_pay_nothing(sm, settings, pool, kw):
    assert ledger.process_killmail(sm, settings, killmail(5, **kw)) is None
    assert active(sm).remaining_pool_isk == 100_000_000


def test_verifier_veto(sm, settings, pool):
    assert ledger.process_killmail(sm, settings, killmail(6), verify=lambda d: False) is None
    assert active(sm).remaining_pool_isk == 100_000_000


def test_new_pool_after_depletion_ignores_old_kills(sm, settings, pool):
    ledger.process_killmail(sm, settings, killmail(7, time="2099-01-01T00:00:00Z"))
    assert active(sm) is None
    fund(sm, settings, BOB, 50_000_000)
    ledger.place_bounty(sm, settings, BOB, TARGET, "Target", 50_000_000, False)
    assert active(sm).id != pool.id
    # a kill from before the new pool existed cannot drain it
    assert ledger.process_killmail(sm, settings, killmail(8, time="2026-01-01T00:00:00Z")) is None


# ------------------------------------------------------------------ concurrency / double spend

def run_threads(fn, args_list):
    results, barrier = [None] * len(args_list), threading.Barrier(len(args_list))

    def worker(i, args):
        barrier.wait()
        try:
            results[i] = fn(*args)
        except Exception as e:  # noqa: BLE001
            results[i] = e

    ts = [threading.Thread(target=worker, args=(i, a)) for i, a in enumerate(args_list)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return results


def test_parallel_duplicate_killmail_pays_once(sm, settings, pool):
    km = killmail(9, value=10_000_000)
    res = run_threads(lambda: ledger.process_killmail(sm, settings, km), [()] * 12)
    assert sum(1 for r in res if isinstance(r, Claim)) == 1
    assert active(sm).remaining_pool_isk == 90_000_000


def test_parallel_different_kills_cannot_overdraw_pool(sm, settings, pool):
    # Each kill alone would take the whole 100M pool.
    res = run_threads(lambda k: ledger.process_killmail(sm, settings, killmail(k)),
                      [(100 + i,) for i in range(10)])
    claims = all_claims(sm)
    assert len(claims) == 1 and sum(c.gross_payout for c in claims) == 100_000_000
    assert not any(isinstance(r, Exception) for r in res), res
    with sm() as s:
        assert s.get(Bounty, pool.id).remaining_pool_isk == 0


def test_parallel_partial_kills_sum_to_pool(sm, settings, pool):
    # 15 kills x 10M on a 100M pool: exactly 10 pay, the pool ends at 0, never negative.
    run_threads(lambda k: ledger.process_killmail(sm, settings, killmail(k, value=10_000_000)),
                [(200 + i,) for i in range(15)])
    claims = all_claims(sm)
    assert len(claims) == 10 and sum(c.gross_payout for c in claims) == 100_000_000


def test_parallel_spend_cannot_overdraw_balance(sm, settings):
    fund(sm, settings, ALICE, 10_000_000)
    res = run_threads(lambda t: ledger.place_bounty(sm, settings, ALICE, t, "T", 10_000_000, False),
                      [(5000 + i,) for i in range(8)])
    assert sum(1 for r in res if isinstance(r, BountyContribution)) == 1
    assert balance(sm, ALICE) == 0


def test_isk_is_conserved(sm, settings):
    for cid in (ALICE, BOB, CAROL):
        fund(sm, settings, cid, 200_000_000)
    ledger.place_bounty(sm, settings, ALICE, TARGET, "T", 60_000_000, False)
    ledger.place_bounty(sm, settings, BOB, TARGET, "T", 45_000_000, True)
    ledger.place_bounty(sm, settings, CAROL, 2002, "U", 5_000_000, False)
    ledger.process_killmail(sm, settings, killmail(300, value=40_000_000))
    with sm() as s:
        deposits = s.scalar(select(func.sum(Deposit.amount_isk)).where(Deposit.status == "CREDITED"))
        balances = s.scalar(select(func.sum(User.wallet_balance)))
        pools = s.scalar(select(func.sum(Bounty.remaining_pool_isk)))
        claims = s.scalar(select(func.sum(Claim.gross_payout)))
        fees = s.scalar(select(func.sum(Claim.host_fee_isk)))
        nets = s.scalar(select(func.sum(Claim.net_payout)))
    assert deposits == balances + pools + claims
    assert claims == fees + nets and fees == 1_000_000


# ------------------------------------------------------------------ payout queue

def test_mark_paid_once(sm, settings, pool):
    c = ledger.process_killmail(sm, settings, killmail(400))
    assert [p.id for p in ledger.pending_payouts(sm)] == [c.id]
    ledger.mark_paid(sm, c.id, 1)
    assert ledger.pending_payouts(sm) == []
    with pytest.raises(ledger.BountyError):
        ledger.mark_paid(sm, c.id, 1)


def test_outgoing_journal_entry_reconciles_payout(sm, settings, pool):
    c = ledger.process_killmail(sm, settings, killmail(401))
    wrong_amount = journal(900, -(c.net_payout - 1), f"CLAIM-{c.id}", sender=RECEIVER, receiver=HUNTER)
    wrong_person = journal(901, -c.net_payout, f"CLAIM-{c.id}", sender=RECEIVER, receiver=HUNTER2)
    right = journal(902, -c.net_payout, f"bounty CLAIM-{c.id}", sender=RECEIVER, receiver=HUNTER)
    res = ledger.record_journal_entries(sm, settings, [wrong_amount, wrong_person])
    assert res["payouts_reconciled"] == 0
    res = ledger.record_journal_entries(sm, settings, [right, right])
    assert res["payouts_reconciled"] == 1
    with sm() as s:
        got = s.get(Claim, c.id)
        assert (got.status, got.payout_journal_ref_id) == ("PAID", 902)
