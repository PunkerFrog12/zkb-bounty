"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int_list(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg://bounty:bounty@localhost:5432/bounty"
    public_url: str = "http://localhost:8000"
    session_secret: str = "change-me"

    # EVE SSO application (https://developers.eveonline.com/)
    sso_client_id: str = ""
    sso_client_secret: str = ""

    # In-game character that receives ISK deposits and sends payouts.
    # Never hard-coded: zKillboard can point this at its existing donation
    # character or at a dedicated bounty character.
    receiving_character_id: int = 0
    admin_character_ids: frozenset[int] = field(default_factory=frozenset)

    # Money rules (all ISK amounts are whole ISK).
    min_bounty_isk: int = 5_000_000
    host_fee_permille: int = 25  # 25 permille = 2.5 %
    # A kill pays min(remaining pool, payout_ratio * ISK destroyed on the kill).
    # 20 mirrors CCP's in-game bounty rule: farming your own bounty with an alt
    # costs you 80%+ of every ship you feed it. 100 = "up to what the target lost".
    payout_ratio_percent: int = 20
    # Fresh-alt defence: the paid hunter must be at least this old on the kill date
    # (ESI birthday) and have this many kills on zKillboard.
    min_hunter_age_days: int = 90
    min_hunter_kills: int = 10
    # Claims wait this long in the admin queue before they can be paid, so an
    # admin (or a placer) can spot a farmed kill and reject it.
    claim_hold_hours: int = 72
    deposit_code_prefix: str = "BNTY-"
    # Credit an un-coded donation to its sender if the sender is a registered
    # user. Only safe when the receiving character is dedicated to bounties.
    match_deposits_by_sender: bool = False

    esi_base: str = "https://esi.evetech.net"
    esi_compat_date: str = "2025-11-06"
    r2z2_base: str = "https://r2z2.zkillboard.com"
    zkb_api_base: str = "https://zkillboard.com/api"
    user_agent: str = "zkb-bounty/0.1 (companion service)"

    @staticmethod
    def from_env() -> "Settings":
        e = os.environ.get
        d = Settings()
        return Settings(
            database_url=e("DATABASE_URL", d.database_url),
            public_url=e("PUBLIC_URL", d.public_url).rstrip("/"),
            session_secret=e("SESSION_SECRET", d.session_secret),
            sso_client_id=e("SSO_CLIENT_ID", ""),
            sso_client_secret=e("SSO_CLIENT_SECRET", ""),
            receiving_character_id=int(e("RECEIVING_CHARACTER_ID", "0")),
            admin_character_ids=_int_list(e("ADMIN_CHARACTER_IDS", "")),
            min_bounty_isk=int(e("MIN_BOUNTY_ISK", str(d.min_bounty_isk))),
            host_fee_permille=int(e("HOST_FEE_PERMILLE", str(d.host_fee_permille))),
            payout_ratio_percent=int(e("PAYOUT_RATIO_PERCENT", str(d.payout_ratio_percent))),
            min_hunter_age_days=int(e("MIN_HUNTER_AGE_DAYS", str(d.min_hunter_age_days))),
            min_hunter_kills=int(e("MIN_HUNTER_KILLS", str(d.min_hunter_kills))),
            claim_hold_hours=int(e("CLAIM_HOLD_HOURS", str(d.claim_hold_hours))),
            deposit_code_prefix=e("DEPOSIT_CODE_PREFIX", d.deposit_code_prefix),
            match_deposits_by_sender=e("MATCH_DEPOSITS_BY_SENDER", "false").lower() == "true",
            esi_base=e("ESI_BASE", d.esi_base),
            esi_compat_date=e("ESI_COMPAT_DATE", d.esi_compat_date),
            r2z2_base=e("R2Z2_BASE", d.r2z2_base),
            zkb_api_base=e("ZKB_API_BASE", d.zkb_api_base),
            user_agent=e("USER_AGENT", d.user_agent),
        )
