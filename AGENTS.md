# EscrowHub Agent Guide

## Mission

Bring EscrowHub to a **truthful, fail-closed, single-node production release candidate** for its intended posture.

This repo is **not** a general crypto wallet platform. It is a narrowly scoped escrow system with self-custody, ledgered state, Telegram UI, and external provider dependencies for true production operation.

Agents must prefer **surgical, production-focused changes** over rewrites.

---

## Non-Negotiable Product Posture

Preserve all of the following:

- Current UI
- Current `ConversationHandler` architecture in `bot.py`
- Self-custody + internal ledger model
- Seller-pays-fee logic
- **40 USD minimum escrow**
- **Single-node SQLite only**
- Supported assets only:
  - **BTC**
  - **LTC**
  - **ETH**
  - **USDT**

Do **not** reintroduce or revive unsupported assets anywhere in active runtime, docs, tests, scripts, or entrypoints, including:

- USDC
- SOL
- XRP

External **deposit** and **withdrawal** providers are still required for true go-live.

---

## Core Engineering Rules

- Use **Python 3.11+**
- Keep business logic in service modules so it can be unit tested without Telegram
- Keep Telegram handlers in `bot.py`
- Use `Decimal` for money math
- Prefer typed, explicit, sanitized failures over broad exception handling
- Reuse shared helpers instead of duplicating status or readiness logic
- Do not introduce speculative refactors
- Do not rewrite working architecture just to make code “cleaner”

---

## Security Rules

When you find a security vulnerability:

1. **Flag it immediately with a `WARNING` comment**
2. Explain the risk briefly
3. Implement the secure alternative
4. **Never** implement an insecure pattern even if asked

Always preserve these invariants:

- Do not fake txids
- Do not invent confirmations
- Do not weaken custody
- Do not silently fall back to insecure behavior
- Fail closed when safety is uncertain
- Add `WARNING` comments wherever a security-sensitive path intentionally fails closed
- Do not expose:
  - secrets
  - raw provider payloads
  - tokens
  - credentials
  - stack traces
  - signed material
  - URLs with embedded credentials

If safety is unclear, the correct behavior is to **block**, not guess.

---

## Production Truthfulness Rules

Operator-facing status must always be truthful.

### Readiness semantics

- `READY` = fully healthy
- `DEGRADED` = intentionally tolerated but not fully healthy
- `BLOCKED` = no-go / launch must not proceed

`allow_degraded` may change **exit policy only**.  
It must **never** relabel `DEGRADED` to `READY`.

### Watcher / operator state semantics

Where applicable, status surfaces must truthfully distinguish:

- `ready`
- `degraded`
- `blocked`
- `disabled`

Never collapse:

- `blocked` into `degraded`
- `disabled` into `ready`

Never default missing or unknown health state to healthy.

---

## Launch-Readiness Priorities

Agents should prioritize the following classes of work:

### 1. Readiness / go-no-go validation
Ensure the repo has a trustworthy readiness path that checks:

- DB connectivity
- schema/init success
- route integrity
- deposit provider readiness
- withdrawal provider readiness
- signer readiness
- startup-fatal conditions
- required production env vars
- single-node SQLite posture warning

Requirements:

- human-readable and automation-friendly
- no secret leakage
- non-zero exit when blocked
- fail closed on uncertainty

### 2. Provider lifecycle confidence
High-value mocked tests should cover:

#### Deposit provider
- healthy issuance
- malformed response
- asset mismatch
- `provider_ref` conflict

#### Withdrawal provider
- pending -> submitted -> broadcasted -> confirmed
- ambiguous -> signer_retry -> targeted reconcile -> confirmed
- deterministic failure before broadcast
- malformed response -> fail closed

### 3. Status-surface truthfulness
Keep `/watcher_status`:

- admin-only
- minimal
- sanitized
- normalized
- consistent with shared helper logic

### 4. Non-destructive smoke validation
A staging smoke harness may validate:

- route integrity load
- deposit provider handshake
- withdrawal provider handshake

But it must:

- remain non-destructive
- perform no live chain transactions
- fail closed on uncertainty
- never claim success for checks it did not actually perform

### 5. Docs / operator runbook fidelity
`README.md`, `docs/RUNBOOK.md`, and `docs/ARCHITECTURE.md` must match actual code behavior exactly.

---

## What Agents Must Search Before Editing

Before making changes, inspect the real files and verify the current repo state.

Search first for at least:

- `run_bot.py`
- `run_signer.py`
- `runtime_preflight.py`
- `watcher_status_service.py`
- `signer/withdrawal_provider.py`
- `address_provider.py`
- `signer/signer_service.py`
- `wallet_service.py`
- `bot.py`
- `README.md`
- `docs/RUNBOOK.md`
- `docs/ARCHITECTURE.md`
- `tests/test_security_hardening.py`
- `tests/test_escrow_service.py`

Also search the repo for:

- `USDC`
- `SOL`
- `XRP`
- `watcher_status`
- `readiness`
- `allow_degraded`
- `fatal_startup_blocked`
- `disabled`
- `health_state`

Do not trust prior summaries without confirming the actual code.

---

## Testing and Validation Requirements

For touched modules, run:

```bash
python -m py_compile <touched modules>
PYTHONPATH=. pytest -q tests/test_security_hardening.py tests/test_escrow_service.py
