-- Migration-ready SQL schema for custody wallet model

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
  bot_extra_fee_percent NUMERIC(10,4) NOT NULL DEFAULT 0 CHECK (bot_extra_fee_percent >= 0 AND bot_extra_fee_percent <= 3),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE wallet_addresses (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  chain_family TEXT NOT NULL,
  address TEXT NOT NULL,
  derivation_index BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, asset)
);

CREATE TABLE xrp_routes (
  user_id BIGINT PRIMARY KEY REFERENCES users(id),
  xrp_receive_address TEXT NOT NULL,
  destination_tag TEXT NOT NULL UNIQUE
);

CREATE TABLE deposits (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  amount NUMERIC(36,18) NOT NULL,
  txid TEXT NOT NULL,
  chain_family TEXT NOT NULL,
  unique_key TEXT NOT NULL UNIQUE,
  confirmations INT NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('seen', 'confirmed', 'credited')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE escrows (
  id BIGSERIAL PRIMARY KEY,
  bot_id BIGINT NOT NULL REFERENCES bots(id),
  buyer_id BIGINT NOT NULL REFERENCES users(id),
  seller_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  amount NUMERIC(36,18) NOT NULL CHECK (amount > 0),
  status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'completed', 'cancelled', 'disputed')),
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE escrow_locks (
  escrow_id BIGINT PRIMARY KEY REFERENCES escrows(id),
  user_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  amount NUMERIC(36,18) NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('locked', 'released'))
);

CREATE TABLE ledger_entries (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT,
  account_type TEXT NOT NULL CHECK (account_type IN ('USER', 'PLATFORM_REVENUE', 'BOT_OWNER_REVENUE')),
  account_owner_id BIGINT,
  asset TEXT NOT NULL,
  amount NUMERIC(36,18) NOT NULL,
  entry_type TEXT NOT NULL CHECK (entry_type IN (
    'DEPOSIT', 'WITHDRAWAL_RESERVE', 'WITHDRAWAL_SENT', 'ESCROW_LOCK',
    'ESCROW_RELEASE', 'PLATFORM_FEE', 'BOT_FEE', 'ADJUSTMENT'
  )),
  ref_type TEXT NOT NULL,
  ref_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE withdrawals (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT,
  asset TEXT NOT NULL,
  amount NUMERIC(36,18) NOT NULL,
  destination_address TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'broadcasted', 'confirmed', 'failed')),
  txid TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE platform_payout_addresses (
  asset TEXT PRIMARY KEY,
  address TEXT NOT NULL
);

CREATE TABLE bot_owner_payout_addresses (
  owner_user_id BIGINT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  address TEXT NOT NULL,
  PRIMARY KEY (owner_user_id, asset)
);
