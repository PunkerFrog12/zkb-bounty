# zkb-bounty: player bounties for zKillboard

A companion service that brings back player-funded bounties on zKillboard:

1. A pilot logs in with **EVE SSO**, searches any character and places a bounty
   (minimum 5,000,000 ISK, no maximum), either under their name or anonymously.
   Several pilots can add to the same target's pool.
2. Bounties are funded from a balance. Pilots deposit ISK in game to the **receiving character**
   (configurable by ID) with their personal deposit code (`BNTY-XXXXXXXX`) in the reason field.
   **ESI wallet polling** credits it.
3. The service follows zKillboard's **R2Z2** kill feed. When a target dies, it checks the kill
   against ESI and creates a claim for the hunter. The host keeps a **2.5% fee**.
4. ESI cannot send ISK, so claims go to an **admin payout queue**. Each claim is held for 72 hours
   so an admin can reject a farmed kill. After that, a human sends the ISK in game and marks the
   claim paid, or the wallet poller sees the outgoing transfer and marks it paid.

## Native or companion? Companion.

I read zKillboard's code at `zKillboard/zKillboard@c39f299` (2026-11-17) before choosing. It is
PHP + MongoDB + Redis, with cron scripts under `cron/` run every minute by `cron.sh`.

| What zKillboard already has | Where | How this service uses it |
|---|---|---|
| ISK donation detection | `cron/9.wallet.php` polls `$adminCharacter`'s wallet journal and turns `player_donation`s into ad-free months | Same approach (ESI journal, `player_donation`), but in its own process with its own token |
| A spend-ISK-on-a-kill feature | `view/sponsor.php` ("sponsored" kills, paid out of ad-free time) | Shows the community already pays ISK for visibility. Bounties are the targeted version |
| Kill ingestion from R2Z2 | `cron/1.r2z2.php` | Follows the same public feed |
| EVE SSO | `ccplogin.php` / `ZKillSSO` | Same SSO, separate client so the scopes stay separate |

Why it isn't built into zKillboard itself:

* **Money needs ACID.** A bounty payout has to debit a pool, insert a claim and enforce
  "one payout per killmail" in one step. zKillboard's `Mdb` helpers (`set`, `insert`, `save`)
  take no session. Mongo transactions appear in only one place, hand-rolled in
  `cron/3.queueProcess.php`. That could be done natively, but every money path would need the
  same hand-rolled session/retry code. Postgres gives row locks plus `UNIQUE`/`CHECK`
  constraints, so the database itself rejects double spends and negative balances.
* **The repo's `AGENTS.md` rules:** "Do not add new Composer, npm, or system dependencies" and
  "Avoid changes that can increase cron load". A ledger inside zKillboard would break both.
* **Risk isolation.** A bug in the bounty code can't affect killmail ingestion, and the wallet
  refresh token (which can read the receiving character's wallet) lives in a separate database.
* **Small footprint in zKillboard:** an nginx `location /bounties/`, an optional badge on
  character pages (`integration/character-badge.html`), and, if you reuse the donation
  character, a 5-line patch (`integration/9.wallet.php.patch`) so bounty deposits don't also
  become ad-free months.

If the owner prefers native PHP later, `bounty/ledger.py` is a self-contained spec. Port it
to PHP with Mongo transactions, following the existing `3.queueProcess.php` pattern, plus
unique indexes on `claims.killmail_id` and `deposits.journal_ref_id`. Keep the tests as the
acceptance criteria.

## Kill feed: R2Z2, not RedisQ

RedisQ has been retired. zKillboard's wiki now calls R2Z2 the "RedisQ replacement", and
zKillboard ingests it itself (`cron/1.r2z2.php`). I checked on 2026-11-20:
`https://r2z2.zkillboard.com/ephemeral/sequence.json` answers, and each `/ephemeral/<seq>.json`
holds the ESI killmail plus the `zkb` block (`totalValue`, `npc`, `awox`, ...). The worker
follows the documented etiquette:

* Read `sequence.json` once, then step through `<seq>.json`. After a 404, wait 6 s. Stay at or
  under 10 req/s (the limit is 15).
* The cursor is stored in Postgres and advances only after a kill has been handled.
* The same killmail can come back under a new sequence (zKillboard re-processes kills).
  `claims.killmail_id UNIQUE` makes that harmless.
* Old kills can be added to zKillboard at any time, so a kill only pays a pool that existed
  before the kill (`killmail_time >= bounty.created_at`).

## Payout rules (decided and documented)

| Rule | Value | Why |
|---|---|---|
| Who is paid | **Final-blow** attacker. If that attacker isn't eligible (an NPC, for example), the eligible attacker with the most damage. If nobody is eligible, nobody is paid and the pool stays | One transfer per kill for the admin to send. Splitting payouts invites "tag the kill with 20 alts" |
| Eligible attacker | A player character, not the victim, not in the victim's corp or alliance | Stops the cheapest alt/blue farming |
| Fresh-alt check | The paid pilot must be at least **90 days old on the kill date** (ESI `birthday`) and have at least **10 kills on zKillboard**. If the final-blow pilot fails, the next eligible pilot by damage is tried | Blocks the classic exploit: the target makes a new alt in another corp, dies to it and collects their own bounty |
| Never pays | NPC kills (`zkb.npc`), awox kills (`zkb.awox`), kills older than the pool | |
| Amount per kill | `min(remaining pool, ISK destroyed × PAYOUT_RATIO_PERCENT/100)`, default **20%** (CCP's in-game number) | Farming your own bounty becomes a losing trade: feed a 100M ship to your alt and get 19.5M back. Raise it toward 100% for faster payouts if abuse turns out to be rare |
| Payout hold | Claims wait **72 hours** (`CLAIM_HOLD_HOURS`) before they can be marked paid. An admin can **reject** a claim during that time; its full ISK goes back to the pool (a drained pool reopens) | Gives humans a window to catch an old, active alt the automatic checks can't |
| Host fee | 2.5% of gross, rounded half-up to whole ISK, `net = gross - fee` | `CHECK (gross = fee + net)` in the database |
| Minimum bounty | 5,000,000 ISK; no maximum; whole ISK only; you can't place a bounty on yourself | |
| Pools | One ACTIVE pool per target (partial unique index). When it reaches 0 it becomes DEPLETED, and the next contribution opens a new pool | |
| Refunds | Placed bounties are final (the page says so). Unspent balance stays on the account | Keeps the ledger simple. Refunds/withdrawals of balance are a later feature |

All ISK amounts are whole ISK stored as `BIGINT`, so there is no floating-point money anywhere.
Deposit amounts with cents are floored.

## Deposits

* Every pilot gets a deposit code on first login. The account page says: *Give Money to
  character `RECEIVING_CHARACTER_ID` with reason `BNTY-XXXXXXXX`*.
* The wallet worker refreshes the receiving character's SSO token and reads
  `GET /characters/{id}/wallet/journal/` (every page). For each incoming `player_donation`:
  * code found in the reason (case-insensitive) → credit that user;
  * otherwise, if `MATCH_DEPOSITS_BY_SENDER=true` (only safe with a dedicated character), credit
    the sender if they have an account;
  * otherwise it is listed as **UNMATCHED** on the admin page, where it can be credited by hand.
* Each journal `id` is stored once (it's the primary key), so polling twice can't credit twice.
* The worker also reads outgoing `player_donation`s with reason `CLAIM-<id>`. If the recipient and
  the exact net amount match, it marks that claim PAID (automatic reconciliation).

## Concurrency and double spend

* `place_bounty`: a conditional `UPDATE users SET wallet_balance = wallet_balance - x WHERE
  wallet_balance >= x`, plus `CHECK (wallet_balance >= 0)`, so a balance can't be spent twice.
* Kills on characters without an active bounty are dropped before any network call, so the
  fresh-alt check (one ESI and one zKillboard request per pilot tried) only runs for bounty kills.
* `process_killmail`: `SELECT ... FOR UPDATE` on the active pool, a conditional decrement
  (`remaining_pool_isk >= gross`), and `INSERT` into `claims` with `killmail_id UNIQUE`, all in
  one transaction. If two workers race on the same kill, the loser hits the unique constraint and
  pays nothing. If two different kills race on one pool, the row lock makes them run one after
  the other, and `CHECK (remaining_pool_isk >= 0)` is the last line of defence.
* Invariant tested: `credited deposits = balances + open pools + claimed gross`.

## Setup

Requirements: Python 3.11+ and PostgreSQL 14+.

```bash
pip install -r requirements.txt
cp .env.example .env    # then fill it in; load it with your process manager or `set -a; . ./.env`
```

1. Create an EVE application at https://developers.eveonline.com/ with callback
   `$PUBLIC_URL/callback` and scope `esi-wallet.read_character_wallet.v1`. Put the client
   ID and secret in `.env`.
2. Set `RECEIVING_CHARACTER_ID` (the in-game character that takes deposits and sends
   payouts) and `ADMIN_CHARACTER_IDS`.
3. Start the web app (it creates the tables; `schema.sql` holds the same DDL for review):

   ```bash
   uvicorn bounty.web:create_app --factory --host 0.0.0.0 --port 8000
   ```
4. Log in as the receiving character at `/login?wallet=1` once. Its refresh token is stored for
   the wallet worker.
5. Run the two workers (systemd, supervisor, or two containers):

   ```bash
   python -m bounty.workers killfeed
   python -m bounty.workers wallet
   ```

### Deploying next to zKillboard

```nginx
location /bounties/ { proxy_pass http://127.0.0.1:8000/; proxy_set_header Host $host; }
```
Set `PUBLIC_URL=https://zkillboard.com/bounties`. Then add `integration/character-badge.html`
to the character page template. If the receiving character is zKillboard's existing donation
character, apply `integration/9.wallet.php.patch`.

## Tests

```bash
python -m pytest -q
```

There are 75 tests, and they pass on both SQLite and PostgreSQL 18:

* fee math, including 10k random amounts and a 1T ISK case;
* minimums, no maximum, whole-ISK only, no bounty on yourself, insufficient balance;
* pooling and anonymity;
* deposits: code match, idempotency, unmatched then assigned, sender match, filters, cents;
* claim rules: final blow, NPC fallback, ratio cap, npc/awox/corp/alliance/self/old kill;
* double-claim prevention, sequential and with 12 threads at once;
* parallel kills on one pool and parallel spending of one balance;
* ISK conservation;
* payout queue and automatic reconciliation;
* the web flow (SSO state check, CSRF, anonymous masking in HTML and JSON, admin-only queue);
* the R2Z2 worker cursor, including a re-sent kill;
* the anti-farming defences: fresh-alt fallback to a veteran attacker, age measured on the kill
  date, zKillboard kill history, the 20% cap, the 72-hour hold, and admin reject returning ISK to
  the pool (including a drained pool reopening).

By default the tests use SQLite. On SQLite every transaction takes the write lock at `BEGIN`
(`BEGIN IMMEDIATE`), so writes run one at a time. To run the same suite, threaded race tests
included, against real Postgres row locks:

```bash
TEST_DATABASE_URL=postgresql+psycopg://bounty:bounty@localhost:5432/bounty_test python -m pytest -q
```

## Layout

```
bounty/config.py     settings from env (no hard-coded character names)
bounty/models.py     schema: users, bounties, bounty_contributions, claims, deposits, kv
bounty/killrules.py  who gets paid for a killmail (pure function)
bounty/ledger.py     every money movement, one DB transaction each
bounty/eve.py        EVE SSO (JWT verified against CCP's JWKS), ESI, R2Z2 clients
bounty/workers.py    R2Z2 kill follower, wallet journal poller
bounty/web.py        FastAPI pages + /api/bounty/{id}
integration/         optional zKillboard-side snippets (badge, 9.wallet.php patch)
schema.sql           Postgres DDL generated from the models
```

## Known limits / next steps

* No defence makes farming impossible. A player with an old, active alt in another corp can still
  claim their own bounty, but at 20% they lose most of every ship they feed it, and the 72-hour
  hold gives the admin time to reject it.
* No database migrations: the schema changed when the anti-farming fields were added, so an
  existing database from the first version needs its `claims` table recreated.
* Bounties don't expire, and unspent balances can't be withdrawn yet. Both would go through the
  same payout queue.
* Payouts are manual by design (ESI has no "send ISK" endpoint). The admin page shows how much
  ISK the receiving wallet must cover (owed + open pools + balances).
* Character search is exact-name via ESI `/universe/ids/`. On zKillboard it would reuse the
  site's own autocomplete.
* Running this needs CCP's consent for ISK-handling third-party services. See the pitch deck's
  risks slide.
