-- PostgreSQL schema for zkb-bounty (generated from bounty/models.py; the app also creates it on start).
-- All ISK amounts are whole ISK in BIGINT.

CREATE TABLE bounties (
	id SERIAL NOT NULL, 
	target_character_id BIGINT NOT NULL, 
	target_name VARCHAR(64) NOT NULL, 
	total_pool_isk BIGINT NOT NULL, 
	remaining_pool_isk BIGINT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	depleted_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_bounty_remaining_nonneg CHECK (remaining_pool_isk >= 0), 
	CONSTRAINT ck_bounty_remaining_le_total CHECK (remaining_pool_isk <= total_pool_isk), 
	CONSTRAINT ck_bounty_status CHECK (status IN ('ACTIVE','DEPLETED'))
);
CREATE INDEX ix_bounties_target_character_id ON bounties (target_character_id);
CREATE UNIQUE INDEX uq_bounty_active_target ON bounties (target_character_id) WHERE status = 'ACTIVE';

CREATE TABLE deposits (
	journal_ref_id BIGINT NOT NULL, 
	sender_character_id BIGINT NOT NULL, 
	amount_isk BIGINT NOT NULL, 
	reason TEXT NOT NULL, 
	journal_date TIMESTAMP WITH TIME ZONE NOT NULL, 
	credited_character_id BIGINT, 
	status VARCHAR(16) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (journal_ref_id)
);

CREATE TABLE kv (
	key VARCHAR(64) NOT NULL, 
	value TEXT NOT NULL, 
	PRIMARY KEY (key)
);

CREATE TABLE users (
	character_id BIGINT NOT NULL, 
	character_name VARCHAR(64) NOT NULL, 
	wallet_balance BIGINT NOT NULL, 
	deposit_code VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (character_id), 
	CONSTRAINT ck_users_balance_nonneg CHECK (wallet_balance >= 0), 
	UNIQUE (deposit_code)
);

CREATE TABLE bounty_contributions (
	id SERIAL NOT NULL, 
	bounty_id INTEGER NOT NULL, 
	placer_character_id BIGINT NOT NULL, 
	amount_isk BIGINT NOT NULL, 
	anonymous BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_contrib_positive CHECK (amount_isk > 0), 
	FOREIGN KEY(bounty_id) REFERENCES bounties (id), 
	FOREIGN KEY(placer_character_id) REFERENCES users (character_id)
);
CREATE INDEX ix_bounty_contributions_bounty_id ON bounty_contributions (bounty_id);
CREATE INDEX ix_bounty_contributions_placer_character_id ON bounty_contributions (placer_character_id);

CREATE TABLE claims (
	id SERIAL NOT NULL, 
	bounty_id INTEGER NOT NULL, 
	killmail_id BIGINT NOT NULL, 
	killmail_hash VARCHAR(64) NOT NULL, 
	killmail_time TIMESTAMP WITH TIME ZONE NOT NULL, 
	killer_character_id BIGINT NOT NULL, 
	gross_payout BIGINT NOT NULL, 
	host_fee_isk BIGINT NOT NULL, 
	net_payout BIGINT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	payable_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	rejected_at TIMESTAMP WITH TIME ZONE, 
	rejected_by_character_id BIGINT, 
	paid_at TIMESTAMP WITH TIME ZONE, 
	paid_by_character_id BIGINT, 
	payout_journal_ref_id BIGINT, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_claim_sum CHECK (gross_payout = host_fee_isk + net_payout), 
	CONSTRAINT ck_claim_positive CHECK (net_payout > 0 AND host_fee_isk >= 0), 
	CONSTRAINT ck_claim_status CHECK (status IN ('PENDING_PAYMENT','PAID','REJECTED')), 
	FOREIGN KEY(bounty_id) REFERENCES bounties (id), 
	UNIQUE (killmail_id), 
	UNIQUE (payout_journal_ref_id)
);
CREATE INDEX ix_claims_killer_character_id ON claims (killer_character_id);
CREATE INDEX ix_claims_bounty_id ON claims (bounty_id);
