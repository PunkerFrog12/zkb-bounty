"""Thin clients for EVE SSO (OAuth2 v2), ESI and zKillboard's R2Z2 kill feed."""
from __future__ import annotations

import base64
import datetime as dt
import secrets
from urllib.parse import urlencode

import httpx
import jwt

from .config import Settings
from .killrules import KillDecision, parse_time

SSO_AUTHORIZE = "https://login.eveonline.com/v2/oauth/authorize"
SSO_TOKEN = "https://login.eveonline.com/v2/oauth/token"
SSO_JWKS = "https://login.eveonline.com/oauth/jwks"
WALLET_SCOPE = "esi-wallet.read_character_wallet.v1"


class EveClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.s = settings
        self.http = http or httpx.Client(timeout=20, headers={"User-Agent": settings.user_agent})
        self._jwks = jwt.PyJWKClient(SSO_JWKS)

    # ---------------- SSO
    def authorize_url(self, redirect_uri: str, scopes: list[str]) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        q = {"response_type": "code", "redirect_uri": redirect_uri, "client_id": self.s.sso_client_id,
             "scope": " ".join(scopes), "state": state}
        return f"{SSO_AUTHORIZE}?{urlencode(q)}", state

    def _token(self, data: dict) -> dict:
        basic = base64.b64encode(f"{self.s.sso_client_id}:{self.s.sso_client_secret}".encode()).decode()
        r = self.http.post(SSO_TOKEN, data=data, headers={"Authorization": f"Basic {basic}",
                                                          "Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        return r.json()

    def exchange_code(self, code: str) -> dict:
        return self._token({"grant_type": "authorization_code", "code": code})

    def refresh(self, refresh_token: str) -> dict:
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def verify_access_token(self, access_token: str) -> dict:
        """Validate the SSO JWT signature/issuer/audience; returns {character_id, name, scopes}."""
        key = self._jwks.get_signing_key_from_jwt(access_token).key
        claims = jwt.decode(access_token, key=key, algorithms=["RS256"],
                            audience=[self.s.sso_client_id, "EVE Online"],
                            issuer=["login.eveonline.com", "https://login.eveonline.com"])
        scp = claims.get("scp", [])
        return {"character_id": int(claims["sub"].split(":")[-1]), "name": claims["name"],
                "scopes": [scp] if isinstance(scp, str) else list(scp)}

    # ---------------- ESI
    def _esi(self, method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
        headers = {"X-Compatibility-Date": self.s.esi_compat_date}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = self.http.request(method, f"{self.s.esi_base}{path}", headers=headers, **kw)
        r.raise_for_status()
        return r

    def character_name(self, character_id: int) -> str | None:
        try:
            return self._esi("GET", f"/characters/{character_id}/").json()["name"]
        except httpx.HTTPStatusError:
            return None

    def resolve_character(self, name: str) -> tuple[int, str] | None:
        chars = self._esi("POST", "/universe/ids/", json=[name]).json().get("characters", [])
        return (int(chars[0]["id"]), chars[0]["name"]) if chars else None

    def wallet_journal(self, character_id: int, access_token: str) -> list[dict]:
        r = self._esi("GET", f"/characters/{character_id}/wallet/journal/", access_token, params={"page": 1})
        rows = r.json()
        for page in range(2, int(r.headers.get("X-Pages", "1")) + 1):
            rows += self._esi("GET", f"/characters/{character_id}/wallet/journal/", access_token,
                              params={"page": page}).json()
        return rows

    def hunter_ok(self, character_id: int, kill_time: dt.datetime) -> bool:
        """Fresh-alt defence: old enough on the kill date and has a kill history on zKillboard."""
        born = parse_time(self._esi("GET", f"/characters/{character_id}/").json()["birthday"])
        if (kill_time - born).days < self.s.min_hunter_age_days:
            return False
        if self.s.min_hunter_kills <= 0:
            return True
        r = self.http.get(f"{self.s.zkb_api_base}/stats/characterID/{character_id}/kills/")
        r.raise_for_status()
        stats = r.json()
        kills = int(stats.get("shipsDestroyed") or 0) if isinstance(stats, dict) else 0
        return kills >= self.s.min_hunter_kills

    def verify_kill(self, d: KillDecision) -> bool:
        """Confirm the R2Z2 data against ESI's signed-by-hash killmail before paying."""
        km = self._esi("GET", f"/killmails/{d.killmail_id}/{d.killmail_hash}/").json()
        attackers = {int(a.get("character_id") or 0) for a in km.get("attackers", [])}
        return (int(km["victim"].get("character_id") or 0) == d.victim_character_id
                and d.killer_character_id in attackers)

    # ---------------- R2Z2 (zKillboard's RedisQ replacement)
    def r2z2_sequence(self) -> int:
        return int(self.http.get(f"{self.s.r2z2_base}/ephemeral/sequence.json").json()["sequence"])

    def r2z2_package(self, sequence: int) -> dict | None:
        r = self.http.get(f"{self.s.r2z2_base}/ephemeral/{sequence}.json")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
