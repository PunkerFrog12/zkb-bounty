"""Database schema. All ISK amounts are whole ISK stored as BIGINT (exact integer math)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text,
    create_engine, event, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    character_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    character_name: Mapped[str] = mapped_column(String(64))
    wallet_balance: Mapped[int] = mapped_column(BigInteger, default=0)
    deposit_code: Mapped[str] = mapped_column(String(32), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (CheckConstraint("wallet_balance >= 0", name="ck_users_balance_nonneg"),)


class Bounty(Base):
    __tablename__ = "bounties"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_character_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_name: Mapped[str] = mapped_column(String(64))
    total_pool_isk: Mapped[int] = mapped_column(BigInteger, default=0)
    remaining_pool_isk: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE | DEPLETED
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    depleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("remaining_pool_isk >= 0", name="ck_bounty_remaining_nonneg"),
        CheckConstraint("remaining_pool_isk <= total_pool_isk", name="ck_bounty_remaining_le_total"),
        CheckConstraint("status IN ('ACTIVE','DEPLETED')", name="ck_bounty_status"),
        # At most one ACTIVE pool per target: all contributions stack into it.
        Index("uq_bounty_active_target", "target_character_id", unique=True,
              postgresql_where=text("status = 'ACTIVE'"), sqlite_where=text("status = 'ACTIVE'")),
    )


class BountyContribution(Base):
    __tablename__ = "bounty_contributions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bounty_id: Mapped[int] = mapped_column(ForeignKey("bounties.id"), index=True)
    placer_character_id: Mapped[int] = mapped_column(ForeignKey("users.character_id"), index=True)
    amount_isk: Mapped[int] = mapped_column(BigInteger)
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (CheckConstraint("amount_isk > 0", name="ck_contrib_positive"),)


class Claim(Base):
    __tablename__ = "claims"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bounty_id: Mapped[int] = mapped_column(ForeignKey("bounties.id"), index=True)
    # UNIQUE: a killmail can pay out at most once, ever (R2Z2 re-sends re-processed kills).
    killmail_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    killmail_hash: Mapped[str] = mapped_column(String(64))
    killmail_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    killer_character_id: Mapped[int] = mapped_column(BigInteger, index=True)
    gross_payout: Mapped[int] = mapped_column(BigInteger)
    host_fee_isk: Mapped[int] = mapped_column(BigInteger)
    net_payout: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="PENDING_PAYMENT")  # | PAID | REJECTED
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Anti-farming hold: the claim cannot be marked paid before this time.
    payable_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    rejected_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by_character_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_by_character_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payout_journal_ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    __table_args__ = (
        CheckConstraint("gross_payout = host_fee_isk + net_payout", name="ck_claim_sum"),
        CheckConstraint("net_payout > 0 AND host_fee_isk >= 0", name="ck_claim_positive"),
        CheckConstraint("status IN ('PENDING_PAYMENT','PAID','REJECTED')", name="ck_claim_status"),
    )


class Deposit(Base):
    """One row per incoming wallet journal entry of the receiving character (idempotent)."""
    __tablename__ = "deposits"
    journal_ref_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    sender_character_id: Mapped[int] = mapped_column(BigInteger)
    amount_isk: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text, default="")
    journal_date: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    credited_character_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # CREDITED | UNMATCHED
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KV(Base):
    """Small key/value store: R2Z2 sequence cursor, receiving character refresh token."""
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


def make_sessionmaker(url: str):
    kwargs = {}
    sqlite = url.startswith("sqlite")
    if sqlite:
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, future=True, **kwargs)
    if sqlite:
        # SQLite (tests / dev only): take the write lock at BEGIN so concurrent
        # transactions serialize like Postgres row locks would.
        @event.listens_for(engine, "connect")
        def _connect(dbapi_conn, _rec):
            dbapi_conn.isolation_level = None
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(engine, "begin")
        def _begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
