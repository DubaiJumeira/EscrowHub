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
  bot_display_name TEXT NOT NULL,
  support_contact TEXT,
  bot_username TEXT UNIQUE,
  bot_token_hash TEXT NOT NULL,
  bot_token_encrypted TEXT,
  bot_extra_fee_percent NUMERIC(10,4) NOT NULL DEFAULT 0 CHECK (bot_extra_fee_percent >= 0 AND bot_extra_fee_percent <= 3),
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE wallet_addresses (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  address TEXT NOT NULL,
  xrp_destination_tag TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, asset)
);

CREATE TABLE deposits (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  address TEXT NOT NULL,
  xrp_destination_tag TEXT,
  amount NUMERIC(36,18) NOT NULL CHECK (amount > 0),
  tx_hash TEXT NOT NULL,
  confirmations INT NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('detected', 'confirmed', 'credited')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tx_hash, user_id)
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
  user_id BIGINT,
  asset TEXT NOT NULL,
  amount NUMERIC(36,18) NOT NULL,
  entry_type TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('credit', 'debit')),
  reference_type TEXT NOT NULL,
  reference_id BIGINT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE escrows (
  id BIGSERIAL PRIMARY KEY,
  bot_id BIGINT NOT NULL REFERENCES bots(id),
  buyer_id BIGINT NOT NULL REFERENCES users(id),
  seller_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  amount NUMERIC(36,18) NOT NULL CHECK (amount > 0),
  platform_fee NUMERIC(36,18) NOT NULL,
  bot_extra_fee NUMERIC(36,18) NOT NULL,
  total_fee NUMERIC(36,18) NOT NULL,
  seller_payout NUMERIC(36,18) NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'completed', 'cancelled', 'disputed')),
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
  to_address TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'signed', 'broadcasted', 'confirmed', 'failed', 'cancelled')),
  tx_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
