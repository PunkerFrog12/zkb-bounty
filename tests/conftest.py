import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bounty import ledger  # noqa: E402
from bounty.config import Settings  # noqa: E402
from bounty.models import Base, make_sessionmaker  # noqa: E402

RECEIVER = 90000001
ADMIN = 1
ALICE, BOB, CAROL = 1001, 1002, 1003
TARGET, HUNTER, HUNTER2 = 2001, 3001, 3002


@pytest.fixture
def settings():
    # Base tests pin ratio 100% and no hold so the money math is easy to read;
    # test_antifarm.py covers the production defaults (20%, 72 h hold, hunter checks).
    return Settings(database_url="", receiving_character_id=RECEIVER, admin_character_ids=frozenset({ADMIN}),
                    session_secret="test", payout_ratio_percent=100, claim_hold_hours=0)


@pytest.fixture
def sm(tmp_path):
    """SQLite file DB by default; set TEST_DATABASE_URL=postgresql+psycopg://... to run on Postgres."""
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    maker = make_sessionmaker(url)
    if url.startswith("postgresql"):
        Base.metadata.drop_all(maker.kw["bind"])
        Base.metadata.create_all(maker.kw["bind"])
    yield maker
    maker.kw["bind"].dispose()


def journal(ref_id, amount, reason="", sender=ALICE, receiver=RECEIVER, ref_type="player_donation"):
    return {"id": ref_id, "date": "2026-11-20T12:00:00Z", "ref_type": ref_type, "amount": float(amount),
            "first_party_id": sender, "second_party_id": receiver, "reason": reason}


def fund(sm, settings, character_id, amount, name=None):
    """Create a user and credit them through the real deposit path."""
    user = ledger.get_or_create_user(sm, settings, character_id, name or f"Pilot {character_id}")
    fund.ref += 1
    ledger.record_journal_entries(sm, settings, [journal(fund.ref, amount, user.deposit_code, sender=character_id)])
    return user


fund.ref = 10_000


def killmail(killmail_id, victim=TARGET, attackers=None, value=1_000_000_000, time="2099-01-01T00:00:00Z",
             npc=False, awox=False, victim_corp=500, victim_alliance=0):
    if attackers is None:
        attackers = [{"character_id": HUNTER, "corporation_id": 600, "damage_done": 100, "final_blow": True}]
    return {
        "killmail_id": killmail_id, "hash": f"h{killmail_id}",
        "esi": {"killmail_id": killmail_id, "killmail_time": time, "attackers": attackers,
                "victim": {"character_id": victim, "corporation_id": victim_corp, "alliance_id": victim_alliance}},
        "zkb": {"totalValue": value, "npc": npc, "awox": awox},
    }
