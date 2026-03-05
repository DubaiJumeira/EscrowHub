-- Migration-ready SQL schema (PostgreSQL style)

CREATE TABLE users (
  id BIGSERIAL PRIMARY KEY,
  telegram_id BIGINT UNIQUE NOT NULL,
  username TEXT,
  is_frozen BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE bots (
  id BIGSERIAL PRIMARY KEY,
  owner_user_id BIGINT NOT NULL REFERENCES users(id),
  display_name TEXT NOT NULL,
  bot_username TEXT UNIQUE,
  bot_token_hash TEXT NOT NULL,
  bot_token_encrypted TEXT,
  service_fee_percent NUMERIC(10,4) NOT NULL CHECK (service_fee_percent >= 0 AND service_fee_percent <= 100),
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE wallet_addresses (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  address TEXT NOT NULL,
  chain_type TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, asset)
);

CREATE TABLE balances (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  available NUMERIC(36,18) NOT NULL DEFAULT 0,
  locked NUMERIC(36,18) NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, asset)
);

CREATE TABLE ledger_entries (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  amount NUMERIC(36,18) NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('credit', 'debit')),
  account_type TEXT NOT NULL CHECK (account_type IN ('available', 'locked', 'platform_revenue', 'owner_revenue')),
  entry_type TEXT NOT NULL,
  reference_type TEXT NOT NULL,
  reference_id BIGINT,
  idempotency_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(idempotency_key)
);

CREATE TABLE escrows (
  id BIGSERIAL PRIMARY KEY,
  tenant_bot_id BIGINT NOT NULL REFERENCES bots(id),
  asset TEXT NOT NULL,
  amount NUMERIC(36,18) NOT NULL CHECK (amount > 0),
  buyer_user_id BIGINT NOT NULL REFERENCES users(id),
  seller_user_id BIGINT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'completed', 'cancelled', 'disputed')),
  description TEXT,
  escrow_fee NUMERIC(36,18) NOT NULL,
  service_fee NUMERIC(36,18) NOT NULL,
  platform_fee NUMERIC(36,18) NOT NULL,
  owner_fee NUMERIC(36,18) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE escrow_participants (
  id BIGSERIAL PRIMARY KEY,
  escrow_id BIGINT NOT NULL REFERENCES escrows(id),
  user_id BIGINT NOT NULL REFERENCES users(id),
  role TEXT NOT NULL CHECK (role IN ('buyer', 'seller', 'owner', 'admin')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(escrow_id, user_id, role)
);

CREATE TABLE escrow_events (
  id BIGSERIAL PRIMARY KEY,
  escrow_id BIGINT NOT NULL REFERENCES escrows(id),
  from_status TEXT,
  to_status TEXT NOT NULL,
  actor_user_id BIGINT REFERENCES users(id),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE disputes (
  id BIGSERIAL PRIMARY KEY,
  escrow_id BIGINT NOT NULL REFERENCES escrows(id),
  opened_by_user_id BIGINT NOT NULL REFERENCES users(id),
  reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'rejected')),
  resolution TEXT,
  resolved_by_admin_id BIGINT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at TIMESTAMPTZ
);

CREATE TABLE withdrawals (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  amount NUMERIC(36,18) NOT NULL CHECK (amount > 0),
  address TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'broadcasted', 'confirmed', 'failed', 'cancelled')),
  tx_hash TEXT,
  requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE admin_actions (
  id BIGSERIAL PRIMARY KEY,
  admin_user_id BIGINT NOT NULL REFERENCES users(id),
  action_type TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id BIGINT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE reputation_stats (
  user_id BIGINT PRIMARY KEY REFERENCES users(id),
  completed_trades BIGINT NOT NULL DEFAULT 0,
  disputes_opened BIGINT NOT NULL DEFAULT 0,
  disputes_lost BIGINT NOT NULL DEFAULT 0,
  reputation_score NUMERIC(8,2) NOT NULL DEFAULT 100,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
