"""Who (if anyone) gets paid for a kill. Pure functions over an R2Z2 package.

Payout rule (documented in README):
  * The killmail's victim must be the bounty target.
  * NPC kills (zkb.npc) and awox kills (zkb.awox) never pay.
  * Eligible attackers are player characters that are not the victim and are not in
    the victim's corporation or alliance (blocks the cheapest alt/blue farming).
  * The final-blow attacker is paid if eligible; otherwise the eligible attacker
    with the most damage done; otherwise nobody is paid and the pool stays intact.
  * One killmail pays one character. We deliberately do not split among attackers:
    splits multiply ISK transfers the admin must send by hand and invite
    "tag the kill with 20 alts" farming.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class KillDecision:
    killmail_id: int
    killmail_hash: str
    killmail_time: dt.datetime
    victim_character_id: int
    killer_character_id: int | None
    isk_destroyed: int
    reason: str  # why killer is None, or "ok"


def parse_time(raw: str) -> dt.datetime:
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))


def decide(package: dict) -> KillDecision:
    esi, zkb = package["esi"], package.get("zkb", {})
    victim = esi["victim"]
    victim_id = int(victim.get("character_id") or 0)
    base = dict(
        killmail_id=int(package["killmail_id"]),
        killmail_hash=str(package.get("hash") or zkb.get("hash") or ""),
        killmail_time=parse_time(esi["killmail_time"]),
        victim_character_id=victim_id,
        isk_destroyed=int(zkb.get("totalValue") or 0),
    )
    if not victim_id:
        return KillDecision(killer_character_id=None, reason="victim is not a character", **base)
    if zkb.get("npc"):
        return KillDecision(killer_character_id=None, reason="npc kill", **base)
    if zkb.get("awox"):
        return KillDecision(killer_character_id=None, reason="awox kill", **base)

    v_corp = int(victim.get("corporation_id") or 0)
    v_alli = int(victim.get("alliance_id") or 0)

    def eligible(a: dict) -> bool:
        cid = int(a.get("character_id") or 0)
        if not cid or cid == victim_id:
            return False
        if v_corp and int(a.get("corporation_id") or 0) == v_corp:
            return False
        if v_alli and int(a.get("alliance_id") or 0) == v_alli:
            return False
        return True

    candidates = [a for a in esi.get("attackers", []) if eligible(a)]
    if not candidates:
        return KillDecision(killer_character_id=None, reason="no eligible attacker", **base)
    final = next((a for a in candidates if a.get("final_blow")), None)
    chosen = final or max(candidates, key=lambda a: int(a.get("damage_done") or 0))
    return KillDecision(killer_character_id=int(chosen["character_id"]), reason="ok", **base)
