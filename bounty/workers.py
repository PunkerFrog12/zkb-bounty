"""Background workers.

    python -m bounty.workers killfeed   # follow R2Z2, create claims
    python -m bounty.workers wallet     # poll the receiving character's wallet journal
"""
from __future__ import annotations

import logging
import sys
import time

from sqlalchemy.orm import sessionmaker

from . import ledger
from .config import Settings
from .eve import EveClient
from .models import KV, make_sessionmaker

log = logging.getLogger("bounty.workers")

R2Z2_CURSOR = "r2z2_next_sequence"
WALLET_REFRESH_TOKEN = "receiving_refresh_token"


def kv_get(sm: sessionmaker, key: str) -> str | None:
    with sm() as s:
        row = s.get(KV, key)
        return row.value if row else None


def kv_set(sm: sessionmaker, key: str, value: str) -> None:
    with sm.begin() as s:
        s.merge(KV(key=key, value=value))


def killfeed_step(sm, settings: Settings, eve: EveClient) -> bool:
    """Process one R2Z2 sequence. Returns False when there is nothing new (caller sleeps 6s)."""
    seq = kv_get(sm, R2Z2_CURSOR)
    seq = int(seq) if seq else eve.r2z2_sequence()
    package = eve.r2z2_package(seq)
    if package is None:
        kv_set(sm, R2Z2_CURSOR, str(seq))
        return False
    claim = ledger.process_killmail(sm, settings, package, verify=eve.verify_kill)
    if claim:
        log.info("claim %s: kill %s pays %s ISK net to %s", claim.id, claim.killmail_id,
                 claim.net_payout, claim.killer_character_id)
    kv_set(sm, R2Z2_CURSOR, str(seq + 1))  # cursor advances only after the kill is handled
    return True


def run_killfeed(sm, settings, eve) -> None:
    while True:
        try:
            if killfeed_step(sm, settings, eve):
                time.sleep(0.1)  # <= 10 req/s, well under R2Z2's 15/s limit
            else:
                time.sleep(6)  # documented polite wait after a 404
        except Exception:  # keep following the feed; the cursor did not advance
            log.exception("killfeed error")
            time.sleep(6)


def wallet_step(sm, settings: Settings, eve: EveClient) -> dict:
    token = kv_get(sm, WALLET_REFRESH_TOKEN)
    if not token:
        raise RuntimeError("receiving character has not granted the wallet scope yet (log in via /login?wallet=1)")
    tok = eve.refresh(token)
    if tok.get("refresh_token") and tok["refresh_token"] != token:
        kv_set(sm, WALLET_REFRESH_TOKEN, tok["refresh_token"])  # refresh tokens rotate
    entries = eve.wallet_journal(settings.receiving_character_id, tok["access_token"])
    return ledger.record_journal_entries(sm, settings, entries)


def run_wallet(sm, settings, eve, interval: int = 300) -> None:
    while True:
        try:
            log.info("wallet: %s", wallet_step(sm, settings, eve))
        except Exception:
            log.exception("wallet error")
        time.sleep(interval)  # ESI caches the journal ~1h; polling faster only costs requests


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = Settings.from_env()
    sm_, eve_ = make_sessionmaker(cfg.database_url), EveClient(cfg)
    {"killfeed": run_killfeed, "wallet": run_wallet}[sys.argv[1]](sm_, cfg, eve_)
