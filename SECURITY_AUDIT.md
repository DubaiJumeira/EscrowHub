# Security audit and hardening notes (March 11, 2026)

This audit focused on the self-hosted `v4` multi-asset branch intended for a single-node VPS.

## Scope

Reviewed and tested:
- deterministic deposit issuance (`hd_wallet.py`, `address_provider.py`, `wallet_service.py`)
- deposit watchers (`BTC`, `LTC`, `ETH`, `USDT`)
- withdrawal address validation and reservation logic
- startup integrity and route consistency checks
- schema migration behavior

## Fixed issues

### 1. Critical: deterministic derivation broke for real Telegram IDs
The original implementation used `user_id` directly as a hardened BIP32 account index:
- BTC: `m/84'/0'/{user_id}'/0/0`
- LTC: `m/84'/2'/{user_id}'/0/0`
- ETH: `m/44'/60'/{user_id}'/0/0`

Real Telegram IDs can exceed the hardened index boundary (`2^31-1`), causing address issuance to fail at runtime.

**Fix:** user IDs are deterministically mapped into `(account, index)`:
- `account = user_id // 2^31`
- `index = user_id % 2^31`

This preserves determinism while keeping both values inside valid BIP32 ranges.

### 2. Critical: production readiness allowed `local_hd`, but derivation still hard-failed in production
The configuration/readiness path accepted `ADDRESS_PROVIDER=local_hd`, but `hd_wallet.py` still refused seed-based derivation in production unless an xpub existed.

**Fix:** production derivation now allows explicit `local_hd`/`local`/`seed` mode and still fails closed for other unsupported production modes.

### 3. High: BTC/LTC withdrawal address validation was too weak
The previous validator accepted many invalid UTXO addresses if they merely had:
- an allowed prefix,
- alphanumeric characters,
- a plausible length.

That could allow user funds to be reserved and potentially sent to malformed addresses.

**Fix:** added checksum-backed validation for:
- Base58Check (legacy/P2SH)
- Bech32/Bech32m (SegWit)

for both BTC and LTC.

## Findings not fully solved in this patch

### ETH public RPC availability
The Ethereum/USDT watcher still depends on a real working JSON-RPC endpoint. Public anonymous endpoints may 403, rate-limit, or disappear.

**Recommendation:** use a project-specific RPC URL before enabling ETH/USDT in production.

### Withdrawal custody remains external-provider territory
The code still intentionally treats withdrawals as disabled-by-default unless a real external withdrawal provider exists.
This hardening pass did not turn the bot into a full custody stack.

## Test evidence

### Static checks
- `python3 -m py_compile $(find . -name '*.py')` ✅

### Added regression tests
Added `tests/test_v4_security_regressions.py` covering:
- production `local_hd` derivation path
- large Telegram ID derivation mapping
- rejection of malformed BTC/LTC addresses
- acceptance of valid BTC/LTC addresses

Result:
- `4 passed`

### Functional spot-checks
Validated deposit fee logic on SQLite memory DB:
- 1 BTC deposit => 0.99 BTC user credit, 0.01 BTC platform revenue ✅

## Residual risks

- Single-node SQLite is operationally simple but not HA.
- Seed-based derivation inside app runtime is acceptable for a first self-hosted version, but still weaker than an HSM / external address service.
- Public RPC endpoints are an availability dependency and should not be treated as production-grade.

## Bottom line

After these fixes, the code is materially safer than the original `v4` zip for a first VPS deployment, especially around deterministic routing and invalid-address handling.

It is **not honest or realistic** to say there are "no more vulnerabilities." This was a careful static review plus focused regression testing, not a formal audit or a live adversarial penetration test.
