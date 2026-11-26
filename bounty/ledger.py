"""All money-moving logic. Every public function runs in exactly one DB transaction.

Invariants (also enforced by CHECK / UNIQUE constraints in models.py):
  * user balances and bounty pools never go negative;
  * a killmail pays out at most once (claims.killmail_id UNIQUE);
  * gross_payout == host_fee_isk + net_payout for every claim;
  * sum(deposits credited) == sum(balances) + sum(contributions)  (no ISK appears/disappears).
"""
from __future__ import annotations

import datetime as dt
import re
import secrets
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .killrules import HunterCheck, KillDecision, decide, parse_time, victim_id
from .models import Bounty, BountyContribution, Claim, Deposit, User, utcnow

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I ambiguity


class BountyError(ValueError):
    pass


# ---------------------------------------------------------------- pure math

def split_fee(gross: int, fee_permille: int) -> tuple[int, int]:
    """(host_fee, net) for a gross payout. Fee rounds half-up to whole ISK."""
    if gross < 0:
        raise BountyError("gross must be non-negative")
    fee = (gross * fee_permille + 500) // 1000
    return fee, gross - fee


def gross_for_kill(remaining_pool: int, isk_destroyed: int, payout_ratio_percent: int) -> int:
    return max(0, min(remaining_pool, isk_destroyed * payout_ratio_percent // 100))


def new_deposit_code(prefix: str) -> str:
    return prefix + "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


# ---------------------------------------------------------------- users

def get_or_create_user(sm: sessionmaker, settings: Settings, character_id: int, name: str) -> User:
    with sm.begin() as s:
        user = s.get(User, character_id)
        if user is None:
            user = User(character_id=character_id, character_name=name,
                        wallet_balance=0, deposit_code=new_deposit_code(settings.deposit_code_prefix))
            s.add(user)
        elif user.character_name != name:
            user.character_name = name
        s.flush()
        return user


# ---------------------------------------------------------------- deposits

def _whole_isk(amount) -> int:
    # ESI returns doubles with 2 decimals; we credit whole ISK (floor) - cents are ignored.
    return int(float(amount))


def record_journal_entries(sm: sessionmaker, settings: Settings, entries: list[dict]) -> dict:
    """Ingest the receiving character's wallet journal. Idempotent per journal ref id.

    Incoming player_donation -> credit by deposit code in the reason (or, if enabled, by sender).
    Outgoing player_donation whose reason contains CLAIM-<id> for the exact net payout -> mark PAID.
    """
    code_re = re.compile(re.escape(settings.deposit_code_prefix) + r"[A-Z0-9]{8}", re.I)
    out = {"credited": 0, "unmatched": 0, "skipped": 0, "payouts_reconciled": 0}
    for e in entries:
        if e.get("ref_type") != "player_donation":
            out["skipped"] += 1
            continue
        amount = _whole_isk(e.get("amount", 0))
        if amount < 0 and int(e.get("first_party_id") or 0) == settings.receiving_character_id:
            out["payouts_reconciled"] += _reconcile_payout(sm, e, -amount)
            continue
        if amount <= 0 or int(e.get("second_party_id") or 0) != settings.receiving_character_id:
            out["skipped"] += 1
            continue
        with sm.begin() as s:
            if s.get(Deposit, int(e["id"])) is not None:
                out["skipped"] += 1
                continue
            sender = int(e.get("first_party_id") or 0)
            reason = str(e.get("reason") or "")
            user = None
            m = code_re.search(reason)
            if m:
                user = s.scalar(select(User).where(User.deposit_code == m.group(0).upper()))
            if user is None and settings.match_deposits_by_sender:
                user = s.get(User, sender)
            dep = Deposit(journal_ref_id=int(e["id"]), sender_character_id=sender, amount_isk=amount,
                          reason=reason, journal_date=parse_time(e["date"]),
                          credited_character_id=user.character_id if user else None,
                          status="CREDITED" if user else "UNMATCHED")
            s.add(dep)
            if user:
                s.execute(update(User).where(User.character_id == user.character_id)
                          .values(wallet_balance=User.wallet_balance + amount))
                out["credited"] += 1
            else:
                out["unmatched"] += 1
    return out


def assign_unmatched_deposit(sm: sessionmaker, journal_ref_id: int, character_id: int) -> None:
    """Admin action: credit an UNMATCHED deposit to a user (e.g. they forgot the code)."""
    with sm.begin() as s:
        dep = s.get(Deposit, journal_ref_id, with_for_update=True)
        if dep is None or dep.status != "UNMATCHED":
            raise BountyError("deposit not found or already credited")
        if s.get(User, character_id) is None:
            raise BountyError("user has never logged in")
        dep.status, dep.credited_character_id = "CREDITED", character_id
        s.execute(update(User).where(User.character_id == character_id)
                  .values(wallet_balance=User.wallet_balance + dep.amount_isk))


def _reconcile_payout(sm: sessionmaker, e: dict, amount: int) -> int:
    m = re.search(r"CLAIM-(\d+)", str(e.get("reason") or ""), re.I)
    if not m:
        return 0
    with sm.begin() as s:
        res = s.execute(update(Claim).where(
            Claim.id == int(m.group(1)), Claim.status == "PENDING_PAYMENT",
            Claim.net_payout == amount,
            Claim.killer_character_id == int(e.get("second_party_id") or 0),
        ).values(status="PAID", paid_at=utcnow(), payout_journal_ref_id=int(e["id"])))
        return res.rowcount


# ---------------------------------------------------------------- placing bounties

def place_bounty(sm: sessionmaker, settings: Settings, placer_id: int, target_id: int,
                 target_name: str, amount: int, anonymous: bool) -> BountyContribution:
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise BountyError("amount must be whole ISK")
    if amount < settings.min_bounty_isk:
        raise BountyError(f"minimum bounty is {settings.min_bounty_isk:,} ISK")
    if target_id == placer_id:
        raise BountyError("you cannot place a bounty on yourself")
    if target_id <= 0:
        raise BountyError("invalid target")
    for attempt in range(2):
        try:
            with sm.begin() as s:
                debited = s.execute(update(User).where(
                    User.character_id == placer_id, User.wallet_balance >= amount,
                ).values(wallet_balance=User.wallet_balance - amount)).rowcount
                if debited != 1:
                    raise BountyError("insufficient balance - deposit ISK first")
                bounty = s.scalar(select(Bounty).where(
                    Bounty.target_character_id == target_id, Bounty.status == "ACTIVE",
                ).with_for_update())
                if bounty is None:
                    bounty = Bounty(target_character_id=target_id, target_name=target_name,
                                    total_pool_isk=0, remaining_pool_isk=0, status="ACTIVE")
                    s.add(bounty)
                    s.flush()  # may raise IntegrityError if another tx created it first
                s.execute(update(Bounty).where(Bounty.id == bounty.id).values(
                    total_pool_isk=Bounty.total_pool_isk + amount,
                    remaining_pool_isk=Bounty.remaining_pool_isk + amount))
                c = BountyContribution(bounty_id=bounty.id, placer_character_id=placer_id,
                                       amount_isk=amount, anonymous=anonymous)
                s.add(c)
                s.flush()
                return c
        except IntegrityError:
            if attempt == 1:
                raise
    raise AssertionError("unreachable")


# ---------------------------------------------------------------- kills -> claims

Verifier = Callable[[KillDecision], bool]


def _aware(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def process_killmail(sm: sessionmaker, settings: Settings, package: dict,
                     verify: Verifier | None = None, hunter_ok: HunterCheck | None = None) -> Claim | None:
    """Turn one R2Z2 package into at most one claim. Safe to call repeatedly / concurrently."""
    target = victim_id(package)
    if not target:
        return None
    with sm() as s:  # cheap pre-check before any network call or write transaction
        if s.scalar(select(Bounty.id).where(Bounty.target_character_id == target,
                                            Bounty.status == "ACTIVE")) is None:
            return None
    d = decide(package, hunter_ok)
    if d.killer_character_id is None:
        return None
    if verify is not None and not verify(d):
        return None
    try:
        with sm.begin() as s:
            bounty = s.scalar(select(Bounty).where(
                Bounty.target_character_id == d.victim_character_id, Bounty.status == "ACTIVE",
            ).with_for_update())
            if bounty is None:
                return None
            if s.scalar(select(Claim.id).where(Claim.killmail_id == d.killmail_id)) is not None:
                return None  # already paid (R2Z2 re-sends re-processed kills)
            if d.killmail_time < _aware(bounty.created_at):
                return None  # kill predates this pool: no back-dated claims
            gross = gross_for_kill(bounty.remaining_pool_isk, d.isk_destroyed, settings.payout_ratio_percent)
            fee, net = split_fee(gross, settings.host_fee_permille)
            if net <= 0:
                return None
            # Conditional decrement: even without the row lock this can never overdraw.
            ok = s.execute(update(Bounty).where(
                Bounty.id == bounty.id, Bounty.status == "ACTIVE", Bounty.remaining_pool_isk >= gross,
            ).values(remaining_pool_isk=Bounty.remaining_pool_isk - gross)).rowcount
            if ok != 1:
                return None
            s.refresh(bounty)
            if bounty.remaining_pool_isk == 0:
                bounty.status, bounty.depleted_at = "DEPLETED", utcnow()
            claim = Claim(bounty_id=bounty.id, killmail_id=d.killmail_id, killmail_hash=d.killmail_hash,
                          killmail_time=d.killmail_time, killer_character_id=d.killer_character_id,
                          gross_payout=gross, host_fee_isk=fee, net_payout=net, status="PENDING_PAYMENT",
                          payable_at=utcnow() + dt.timedelta(hours=settings.claim_hold_hours))
            s.add(claim)
            s.flush()
            return claim
    except IntegrityError:
        return None  # lost a race on claims.killmail_id: the other transaction paid it


# ---------------------------------------------------------------- admin payout queue

def pending_payouts(sm: sessionmaker) -> list[Claim]:
    with sm() as s:
        return list(s.scalars(select(Claim).where(Claim.status == "PENDING_PAYMENT").order_by(Claim.id)))


def mark_paid(sm: sessionmaker, claim_id: int, admin_character_id: int) -> None:
    with sm.begin() as s:
        claim = s.get(Claim, claim_id, with_for_update=True)
        if claim is None or claim.status != "PENDING_PAYMENT":
            raise BountyError("claim not found or already settled")
        if utcnow() < _aware(claim.payable_at):
            raise BountyError(f"claim is on hold until {_aware(claim.payable_at):%Y-%m-%d %H:%M} UTC")
        claim.status, claim.paid_at, claim.paid_by_character_id = "PAID", utcnow(), admin_character_id


def reject_claim(sm: sessionmaker, claim_id: int, admin_character_id: int) -> None:
    """Admin action for a farmed or bogus kill: cancel the claim and return its full gross to the pool."""
    with sm.begin() as s:
        claim = s.get(Claim, claim_id, with_for_update=True)
        if claim is None or claim.status != "PENDING_PAYMENT":
            raise BountyError("claim not found or already settled")
        bounty = s.get(Bounty, claim.bounty_id, with_for_update=True)
        if bounty.status == "DEPLETED":
            newer = s.scalar(select(Bounty).where(Bounty.target_character_id == bounty.target_character_id,
                                                  Bounty.status == "ACTIVE").with_for_update())
            if newer is not None:  # a new pool opened meanwhile: the ISK joins it
                newer.total_pool_isk += claim.gross_payout
                newer.remaining_pool_isk += claim.gross_payout
            else:  # reopen the pool the claim drained
                bounty.status, bounty.depleted_at = "ACTIVE", None
                bounty.remaining_pool_isk += claim.gross_payout
        else:
            bounty.remaining_pool_isk += claim.gross_payout
        claim.status, claim.rejected_at, claim.rejected_by_character_id = "REJECTED", utcnow(), admin_character_id
