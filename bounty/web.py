"""FastAPI app: EVE SSO login, search, character bounty page, account, admin payout queue, JSON API."""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from . import ledger
from .config import Settings
from .eve import WALLET_SCOPE, EveClient
from .models import Bounty, BountyContribution, Claim, Deposit, User, make_sessionmaker
from .workers import WALLET_REFRESH_TOKEN, kv_set

_SUFFIX = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_isk(raw: str) -> int:
    """'5000000', '5,000,000', '5m', '1.5b' -> whole ISK."""
    m = re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*([kmb]?)\s*(?:isk)?\s*", raw.lower())
    if not m:
        raise ledger.BountyError("could not read that ISK amount")
    whole, _, frac = m.group(1).replace(",", "").partition(".")
    mult = _SUFFIX[m.group(2)]
    return int(whole) * mult + (int(frac) * mult) // (10 ** len(frac)) if frac else int(whole) * mult


def create_app(settings: Settings | None = None, sm=None, eve: EveClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    sm = sm or make_sessionmaker(settings.database_url)
    eve = eve or EveClient(settings)
    app = FastAPI(title="zKillboard Bounties")
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax",
                       https_only=settings.public_url.startswith("https"))
    tpl = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    tpl.env.filters["isk"] = lambda v: f"{int(v or 0):,}"

    def me(request: Request) -> int:
        return int(request.session.get("character_id") or 0)

    def render(request: Request, name: str, **ctx):
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(24)
        cid = me(request)
        ctx.update(request=request, me=cid, me_name=request.session.get("name"),
                   is_admin=cid in settings.admin_character_ids, csrf=request.session["csrf"],
                   settings=settings, flash=request.session.pop("flash", None))
        return tpl.TemplateResponse(request, name, ctx)

    def check_csrf(request: Request, token: str):
        if not token or token != request.session.get("csrf"):
            raise HTTPException(400, "bad csrf token")

    def redirect(url: str, flash: str | None = None, request: Request | None = None):
        if flash and request is not None:
            request.session["flash"] = flash
        return RedirectResponse(url, status_code=303)

    # ---------------- auth
    @app.get("/login")
    def login(request: Request, wallet: int = 0):
        scopes = [WALLET_SCOPE] if wallet else []
        url, state = eve.authorize_url(f"{settings.public_url}/callback", scopes)
        request.session["oauth_state"] = state
        return RedirectResponse(url)

    @app.get("/callback")
    def callback(request: Request, code: str, state: str):
        if state != request.session.pop("oauth_state", None):
            raise HTTPException(400, "bad oauth state")
        tok = eve.exchange_code(code)
        info = eve.verify_access_token(tok["access_token"])
        ledger.get_or_create_user(sm, settings, info["character_id"], info["name"])
        request.session.update(character_id=info["character_id"], name=info["name"])
        if info["character_id"] == settings.receiving_character_id and WALLET_SCOPE in info["scopes"]:
            kv_set(sm, WALLET_REFRESH_TOKEN, tok["refresh_token"])
            return redirect("/admin", "Wallet access stored for the receiving character.", request)
        return redirect("/account")

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return redirect("/")

    # ---------------- public pages
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with sm() as s:
            top = list(s.scalars(select(Bounty).where(Bounty.status == "ACTIVE")
                                 .order_by(Bounty.remaining_pool_isk.desc()).limit(25)))
        return render(request, "index.html", bounties=top)

    @app.get("/search")
    def search(request: Request, q: str):
        q = q.strip()
        if q.isdigit():
            return redirect(f"/character/{int(q)}")
        hit = eve.resolve_character(q) if q else None
        if not hit:
            return redirect("/", f"No character named '{q}'.", request)
        return redirect(f"/character/{hit[0]}")

    def bounty_view(s, target_id: int) -> dict:
        active = s.scalar(select(Bounty).where(Bounty.target_character_id == target_id, Bounty.status == "ACTIVE"))
        contribs, claims = [], []
        if active:
            rows = s.execute(select(BountyContribution, User.character_name)
                             .join(User, User.character_id == BountyContribution.placer_character_id)
                             .where(BountyContribution.bounty_id == active.id)
                             .order_by(BountyContribution.created_at.desc())).all()
            # Anonymous placers are masked server-side: their id/name never leave the DB.
            contribs = [{"amount": c.amount_isk, "created_at": c.created_at,
                         "name": None if c.anonymous else name,
                         "character_id": None if c.anonymous else c.placer_character_id} for c, name in rows]
        claims = list(s.scalars(select(Claim).join(Bounty, Bounty.id == Claim.bounty_id)
                                .where(Bounty.target_character_id == target_id)
                                .order_by(Claim.id.desc()).limit(20)))
        return {"active": active, "contributions": contribs, "claims": claims}

    @app.get("/character/{target_id}", response_class=HTMLResponse)
    def character(request: Request, target_id: int):
        name = eve.character_name(target_id)
        if name is None:
            raise HTTPException(404, "unknown character")
        with sm() as s:
            view = bounty_view(s, target_id)
            user = s.get(User, me(request)) if me(request) else None
        return render(request, "character.html", target_id=target_id, target_name=name, user=user, **view)

    @app.post("/character/{target_id}/bounty")
    def place(request: Request, target_id: int, amount: str = Form(...), anonymous: str = Form(""),
              csrf: str = Form("")):
        check_csrf(request, csrf)
        if not me(request):
            return redirect("/login")
        name = eve.character_name(target_id)
        if name is None:
            raise HTTPException(404, "unknown character")
        try:
            isk = parse_isk(amount)
            ledger.place_bounty(sm, settings, me(request), target_id, name, isk, anonymous == "1")
            msg = f"Bounty of {isk:,} ISK placed on {name}."
        except ledger.BountyError as e:
            msg = str(e)
        return redirect(f"/character/{target_id}", msg, request)

    @app.get("/account", response_class=HTMLResponse)
    def account(request: Request):
        if not me(request):
            return redirect("/login")
        with sm() as s:
            user = s.get(User, me(request))
            deposits = list(s.scalars(select(Deposit).where(Deposit.credited_character_id == me(request))
                                      .order_by(Deposit.journal_date.desc()).limit(50)))
            placed = s.execute(select(BountyContribution, Bounty.target_name, Bounty.target_character_id)
                               .join(Bounty).where(BountyContribution.placer_character_id == me(request))
                               .order_by(BountyContribution.id.desc()).limit(50)).all()
            won = list(s.scalars(select(Claim).where(Claim.killer_character_id == me(request))
                                 .order_by(Claim.id.desc()).limit(50)))
        return render(request, "account.html", user=user, deposits=deposits, placed=placed, won=won)

    # ---------------- admin payout queue
    def require_admin(request: Request) -> int:
        if me(request) not in settings.admin_character_ids:
            raise HTTPException(403, "admins only")
        return me(request)

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request):
        require_admin(request)
        with sm() as s:
            pending = s.execute(select(Claim, Bounty.target_name).join(Bounty)
                                .where(Claim.status == "PENDING_PAYMENT").order_by(Claim.id)).all()
            unmatched = list(s.scalars(select(Deposit).where(Deposit.status == "UNMATCHED")
                                       .order_by(Deposit.journal_date)))
            stats = {
                "fees": s.scalar(select(func.coalesce(func.sum(Claim.host_fee_isk), 0))),
                "owed": s.scalar(select(func.coalesce(func.sum(Claim.net_payout), 0))
                                 .where(Claim.status == "PENDING_PAYMENT")),
                "pools": s.scalar(select(func.coalesce(func.sum(Bounty.remaining_pool_isk), 0))
                                  .where(Bounty.status == "ACTIVE")),
                "balances": s.scalar(select(func.coalesce(func.sum(User.wallet_balance), 0))),
            }
        return render(request, "admin.html", pending=pending, unmatched=unmatched, stats=stats)

    @app.post("/admin/claims/{claim_id}/paid")
    def admin_paid(request: Request, claim_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        try:
            ledger.mark_paid(sm, claim_id, require_admin(request))
            msg = f"Claim {claim_id} marked paid."
        except ledger.BountyError as e:
            msg = str(e)
        return redirect("/admin", msg, request)

    @app.post("/admin/deposits/{ref_id}/assign")
    def admin_assign(request: Request, ref_id: int, character_id: int = Form(...), csrf: str = Form("")):
        check_csrf(request, csrf)
        require_admin(request)
        try:
            ledger.assign_unmatched_deposit(sm, ref_id, character_id)
            msg = f"Deposit {ref_id} credited to {character_id}."
        except ledger.BountyError as e:
            msg = str(e)
        return redirect("/admin", msg, request)

    # ---------------- JSON API (for a badge on zKillboard character pages)
    @app.get("/api/bounty/{target_id}")
    def api_bounty(target_id: int):
        with sm() as s:
            v = bounty_view(s, target_id)
        a = v["active"]
        return JSONResponse({
            "character_id": target_id,
            "active": bool(a),
            "remaining_pool_isk": a.remaining_pool_isk if a else 0,
            "total_pool_isk": a.total_pool_isk if a else 0,
            "contributors": len(v["contributions"]),
            "url": f"{settings.public_url}/character/{target_id}",
        }, headers={"Cache-Control": "public, max-age=60"})

    return app
# Run with:  uvicorn bounty.web:create_app --factory
