from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP
import base64
import hashlib
import json
import logging
import os
import sqlite3

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config.settings import Settings
from address_provider import build_address_provider
from hd_wallet import HDWalletDeriver
from ledger_service import LedgerService
from price_service import CoinGeckoPriceService, StaticPriceService
from error_sanitizer import sanitize_runtime_error

_AEAD_UNSET = object()

SUPPORTED_ASSETS = set(Settings.supported_assets)
LOGGER = logging.getLogger(__name__)

NETWORK_LABELS = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "LTC": "Litecoin",
    "USDT": "Ethereum (ERC-20)",
    "SOL": "Solana",
}


ASSET_QUANTA = {
    "BTC": Decimal("0.00000001"),
    "LTC": Decimal("0.00000001"),
    "ETH": Decimal("0.00000001"),
    "USDT": Decimal("0.000001"),
    "SOL": Decimal("0.000000001"),
}


@dataclass
class DepositRoute:
    address: str
    asset: str
    chain_family: str
    destination_tag: str | None
    derivation_path: str | None


class WalletService:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.ledger = LedgerService(conn)
        self.hd = HDWalletDeriver()
        self.address_provider = build_address_provider()
        self.price_service = CoinGeckoPriceService(ttl_seconds=60)
        self._fallback_price_service = StaticPriceService({"BTC": Decimal("65000"), "ETH": Decimal("3500"), "LTC": Decimal("80"), "USDT": Decimal("1"), "SOL": Decimal("100")})
        self._aead_cached: AESGCM | None | object = _AEAD_UNSET

    @staticmethod
    def _asset(asset: str) -> str:
        symbol = asset.upper()
        if symbol not in SUPPORTED_ASSETS:
            raise ValueError(f"unsupported asset: {symbol}")
        return symbol

    def _chain_family(self, asset: str) -> str:
        symbol = self._asset(asset)
        if symbol in {"ETH", "USDT"}:
            return "ETHEREUM"
        if symbol == "SOL":
            return "SOLANA"
        return symbol

    def _asset_quantum(self, asset: str) -> Decimal:
        return ASSET_QUANTA.get(self._asset(asset), Decimal("0.00000001"))

    def _quantize_asset(self, asset: str, amount: Decimal) -> Decimal:
        return Decimal(amount).quantize(self._asset_quantum(asset), rounding=ROUND_HALF_UP)

    def asset_network_label(self, asset: str) -> str:
        return NETWORK_LABELS.get(self._asset(asset), self._asset(asset))

    def withdrawal_minimum_usd(self) -> Decimal:
        value = Decimal(str(Settings.withdrawal_minimum_usd or "10"))
        if value <= Decimal("0"):
            raise ValueError("WITHDRAWAL_MINIMUM_USD must be positive")
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _usd_price(self, asset: str) -> Decimal:
        symbol = self._asset(asset)
        try:
            return self.price_service.get_usd_price(symbol)
        except Exception:
            return self._fallback_price_service.get_usd_price(symbol)

    def withdrawal_minimum_asset_amount(self, asset: str) -> Decimal:
        symbol = self._asset(asset)
        price_usd = self._usd_price(symbol)
        if price_usd <= Decimal("0"):
            raise RuntimeError(f"price unavailable for {symbol}")
        raw_amount = self.withdrawal_minimum_usd() / price_usd
        return Decimal(raw_amount).quantize(self._asset_quantum(symbol), rounding=ROUND_UP)

    def deposit_platform_fee(self, asset: str, gross_amount: Decimal) -> Decimal:
        symbol = self._asset(asset)
        pct = Decimal(str(Settings.deposit_platform_fee_percent or "1"))
        fee = self._quantize_asset(symbol, Decimal(gross_amount) * pct / Decimal("100"))
        gross = self._quantize_asset(symbol, Decimal(gross_amount))
        if fee < Decimal("0"):
            fee = Decimal("0")
        if fee > gross:
            fee = gross
        return fee

    def deposit_net_credit(self, asset: str, gross_amount: Decimal) -> Decimal:
        symbol = self._asset(asset)
        gross = self._quantize_asset(symbol, Decimal(gross_amount))
        net = gross - self.deposit_platform_fee(symbol, gross)
        if net < Decimal("0"):
            net = Decimal("0")
        return self._quantize_asset(symbol, net)

    def withdrawal_platform_fee(self, asset: str, amount: Decimal) -> Decimal:
        symbol = self._asset(asset)
        pct = Decimal(str(Settings.withdrawal_platform_fee_percent or "1"))
        fee = self._quantize_asset(symbol, Decimal(amount) * pct / Decimal("100"))
        if fee < Decimal("0"):
            fee = Decimal("0")
        return fee

    def withdrawal_total_debit(self, asset: str, amount: Decimal) -> Decimal:
        symbol = self._asset(asset)
        return self._quantize_asset(symbol, Decimal(amount) + self.withdrawal_platform_fee(symbol, amount))

    def _withdrawal_fee_booked(self, withdrawal_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM ledger_entries WHERE account_type='PLATFORM_REVENUE' AND ref_type='withdrawal' AND ref_id=? AND entry_type='WITHDRAWAL_PLATFORM_FEE' LIMIT 1",
            (int(withdrawal_id),),
        ).fetchone()
        return bool(row)

    def _book_withdrawal_platform_fee_if_needed(self, withdrawal_id: int) -> None:
        if self._withdrawal_fee_booked(withdrawal_id):
            return
        row = self.conn.execute(
            "SELECT asset, platform_fee_amount FROM withdrawals WHERE id=?",
            (int(withdrawal_id),),
        ).fetchone()
        if not row:
            return
        fee = Decimal(str(row["platform_fee_amount"] or "0"))
        if fee <= Decimal("0"):
            return
        self.ledger.add_entry("PLATFORM_REVENUE", None, None, row["asset"], fee, "WITHDRAWAL_PLATFORM_FEE", "withdrawal", int(withdrawal_id))

    def _ensure_user_row(self, user_ref: int) -> int:
        by_id = self.conn.execute("SELECT id FROM users WHERE id=?", (user_ref,)).fetchone()
        if by_id:
            return int(by_id["id"])
        by_tg = self.conn.execute("SELECT id FROM users WHERE telegram_id=?", (user_ref,)).fetchone()
        if by_tg:
            return int(by_tg["id"])
        cur = self.conn.execute("INSERT INTO users(telegram_id, username, frozen) VALUES(?,?,0)", (user_ref, None))
        return int(cur.lastrowid)


    def _aead(self):
        if self._aead_cached is not _AEAD_UNSET:
            return self._aead_cached
        key = (Settings.encryption_key or "").strip()
        if not key:
            if Settings.is_production:
                # WARNING: Production without ENCRYPTION_KEY is blocked to prevent storing plaintext secrets.
                # Secure alternative: set a strong ENCRYPTION_KEY via secret manager before startup.
                raise RuntimeError("ENCRYPTION_KEY is required in production")
            self._aead_cached = None
            return None
        raw = hashlib.pbkdf2_hmac(
            "sha256",
            key.encode(),
            b"EscrowHub::field-encryption::v2",
            max(600000, int(Settings.encryption_kdf_iterations)),
            dklen=32,
        )
        self._aead_cached = AESGCM(raw)
        return self._aead_cached

    def _legacy_aead(self):
        key = (Settings.encryption_key or "").strip()
        if not key:
            return None
        return AESGCM(hashlib.sha256(key.encode()).digest())

    def _encrypt_field(self, value: str) -> str:
        aead = self._aead()
        if aead is None:
            return value
        nonce = os.urandom(12)
        ct = aead.encrypt(nonce, value.encode(), None)
        payload = base64.b64encode(nonce + ct).decode()
        return f"encv2:{payload}"

    def _decrypt_field(self, value: str | None) -> str | None:
        if value is None:
            return None
        if not str(value).startswith(("enc:", "encv2:")):
            return value
        marker = "encv2:" if str(value).startswith("encv2:") else "enc:"
        aead = self._aead() if marker == "encv2:" else self._legacy_aead()
        if aead is None:
            raise RuntimeError("encrypted data present but ENCRYPTION_KEY is not configured")
        raw = base64.b64decode(str(value)[len(marker):].encode())
        nonce, ct = raw[:12], raw[12:]
        return aead.decrypt(nonce, ct, None).decode()

    def _normalize_address_for_chain(self, chain_family: str, address: str) -> str:
        candidate = (address or "").strip()
        if not candidate:
            raise RuntimeError("empty wallet address")
        family = str(chain_family or "").upper().strip()
        if family not in {"BTC", "LTC", "ETHEREUM", "SOLANA"}:
            raise RuntimeError(f"unsupported chain_family: {family}")
        self._validate_withdrawal_address({"ETHEREUM": "ETH", "SOLANA": "SOL"}.get(family, family), candidate)
        if family == "ETHEREUM":
            from eth_utils import to_checksum_address

            return str(to_checksum_address(candidate))
        if family == "SOLANA":
            return candidate
        return candidate

    def _address_fingerprint(self, chain_family: str, normalized_address: str) -> str:
        payload = f"{chain_family}:{normalized_address}".encode()
        return hashlib.sha256(payload).hexdigest()

    def _parsed_route_row(self, row) -> tuple[DepositRoute, str, str]:
        chain_family = str(row["chain_family"])
        decrypted = self._decrypt_field(row["address"])
        if not decrypted:
            raise RuntimeError(f"wallet route id={row['id']} has empty decrypted address")
        normalized = self._normalize_address_for_chain(chain_family, decrypted)
        fingerprint = self._address_fingerprint(chain_family, normalized)
        stored_fingerprint = str(row["address_fingerprint"] or "") if "address_fingerprint" in row.keys() else ""
        if stored_fingerprint and stored_fingerprint != fingerprint:
            # WARNING: Fingerprint mismatch indicates tampering/corruption risk in deposit routing state.
            # Secure alternative: stop startup, audit row history, and repair with an operator-approved migration.
            raise RuntimeError(
                f"wallet route id={row['id']} fingerprint mismatch; refusing startup until remediated"
            )
        route = DepositRoute(
            normalized,
            row["asset"],
            chain_family,
            row["destination_tag"],
            self._decrypt_field(row["derivation_path"]),
        )
        return route, normalized, fingerprint

    def monitored_deposit_address_map(self, assets: list[str]) -> dict[str, int]:
        normalized = [self._asset(a) for a in assets]
        placeholders = ",".join("?" for _ in normalized)
        rows = self.conn.execute(
            f"SELECT id,address,address_fingerprint,asset,chain_family,user_id,destination_tag,derivation_path FROM wallet_addresses WHERE asset IN ({placeholders})",
            tuple(normalized),
        ).fetchall()
        out: dict[str, int] = {}
        seen_route_keys: dict[tuple[str, str], int] = {}
        for row in rows:
            route, normalized_addr, fingerprint = self._parsed_route_row(row)
            key = (route.chain_family, normalized_addr)
            if key in seen_route_keys and seen_route_keys[key] != int(row["user_id"]):
                # WARNING: Duplicate decrypted deposit route across users can misroute credits.
                # Secure alternative: fail closed and force operator remediation before watcher startup.
                raise RuntimeError(
                    f"duplicate monitored deposit route chain_family={route.chain_family} address={normalized_addr}; "
                    "resolve wallet_addresses collision before startup"
                )
            seen_route_keys[key] = int(row["user_id"])
            out[normalized_addr] = int(row["user_id"])
        return out

    def ensure_wallet_route_integrity(self) -> None:
        rows = self.conn.execute(
            "SELECT id,user_id,asset,chain_family,address,address_fingerprint,provider_origin,provider_ref,destination_tag,derivation_path FROM wallet_addresses ORDER BY id ASC"
        ).fetchall()
        by_fingerprint: dict[tuple[str, str], tuple[int, str, int]] = {}
        by_provider: dict[tuple[str, str], tuple[int, str, int]] = {}
        pending_updates: list[tuple[str, int]] = []
        for row in rows:
            route, _, fingerprint = self._parsed_route_row(row)
            stored_fingerprint = str(row["address_fingerprint"] or "")
            if not stored_fingerprint:
                pending_updates.append((fingerprint, int(row["id"])))
            key = (route.chain_family, fingerprint)
            prior = by_fingerprint.get(key)
            if prior:
                current_user_id = int(row["user_id"])
                current_asset = str(row["asset"])
                allowed_shared_eth_route = (
                    route.chain_family == "ETHEREUM"
                    and prior[0] == current_user_id
                    and {prior[1], current_asset}.issubset({"ETH", "USDT"})
                )
                if not allowed_shared_eth_route and (prior[0] != current_user_id or prior[1] != current_asset):
                    raise RuntimeError(
                        "wallet fingerprint collision detected across rows; "
                        f"chain_family={route.chain_family} fingerprint={fingerprint} conflicting_row_ids={prior[2]},{int(row['id'])}. "
                        "Remediation: remove/reassign duplicated deposit route and restart."
                    )
            by_fingerprint[key] = (int(row["user_id"]), str(row["asset"]), int(row["id"]))

            origin = str(row["provider_origin"] or "")
            ref = str(row["provider_ref"] or "")
            if origin and ref:
                provider_key = (origin, ref)
                provider_prior = by_provider.get(provider_key)
                if provider_prior and (
                    provider_prior[0] != int(row["user_id"]) or provider_prior[1] != fingerprint
                ):
                    raise RuntimeError(
                        "provider_ref rebound detected; "
                        f"origin={origin} ref={ref} conflicting_row_ids={provider_prior[2]},{int(row['id'])}. "
                        "Remediation: keep one canonical route and purge the conflicting row."
                    )
                by_provider[provider_key] = (int(row["user_id"]), fingerprint, int(row["id"]))
        if pending_updates:
            self.conn.executemany("UPDATE wallet_addresses SET address_fingerprint=? WHERE id=?", pending_updates)

    def _assert_not_frozen(self, resolved_user_id: int) -> None:
        row = self.conn.execute("SELECT frozen FROM users WHERE id=?", (resolved_user_id,)).fetchone()
        if row and int(row["frozen"]):
            raise ValueError("account is frozen")

    def verify_address_derivation_consistency(self, sample_size: int | None = 25) -> None:
        mismatches: list[str] = []
        checked_legacy_mode = False
        last_id = 0
        remaining = None if sample_size is None else int(sample_size)

        while True:
            limit = 500 if remaining is None else min(500, remaining)
            if limit <= 0:
                break
            rows = self.conn.execute(
                "SELECT id, user_id, asset, address FROM wallet_addresses WHERE asset IN ('BTC','LTC','ETH','USDT','SOL') AND id>? ORDER BY id ASC LIMIT ?",
                (last_id, limit),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                last_id = int(row["id"])
                asset = str(row["asset"])
                user_id = int(row["user_id"])
                stored = self._decrypt_field(row["address"])
                provider_origin = str(row["provider_origin"] or "") if "provider_origin" in row.keys() else ""
                if provider_origin and provider_origin not in {"legacy_seed", "local_hd"}:
                    continue
                if not checked_legacy_mode:
                    self.hd.validate_xpub_configuration()
                    checked_legacy_mode = True
                if asset == "BTC":
                    derived = self.hd.derive_btc_address(user_id).public_address
                elif asset == "LTC":
                    derived = self.hd.derive_ltc_address(user_id).public_address
                elif asset == "SOL":
                    derived = self.hd.derive_sol_address(user_id).public_address
                else:
                    derived = self.hd.derive_eth_address(user_id).public_address
                if stored and stored != derived:
                    mismatches.append(f"asset={asset} user_id={user_id}")
            if remaining is not None:
                remaining -= len(rows)

        if mismatches:
            # WARNING: Derivation mismatch means watcher routing may miss deposits after seed/xpub migration.
            # Secure alternative: migrate addresses with explicit per-row derivation metadata and verified replay before production startup.
            raise RuntimeError("wallet derivation mismatch detected; abort startup and run migration: " + ", ".join(mismatches[:5]))


    def _validate_deposit_address(self, asset: str, deposit_address: str) -> None:
        symbol = self._asset(asset)
        address = (deposit_address or "").strip()
        if not address:
            raise RuntimeError(f"address provider returned empty {symbol} deposit address")
        try:
            self._validate_withdrawal_address(symbol, address)
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError(f"address provider returned invalid {symbol} deposit address") from exc

    def _route_from_row(self, row) -> DepositRoute:
        route, _, _ = self._parsed_route_row(row)
        return route

    def assert_startup_deposit_issuance_ready(self) -> None:
        if not Settings.is_production:
            return
        ready, error = self.address_provider.is_ready()
        if not ready:
            raise RuntimeError(f"Production deposit issuance unavailable: {error or 'external address provider is not ready'}")

    def get_or_create_deposit_address(self, user_id: int, asset: str) -> DepositRoute:
        symbol = self._asset(asset)
        resolved_user_id = self._ensure_user_row(user_id)
        row = self.conn.execute("SELECT * FROM wallet_addresses WHERE user_id=? AND asset=?", (resolved_user_id, symbol)).fetchone()
        if row:
            return self._route_from_row(row)

        issued = None
        issued_normalized = ""
        issued_fingerprint = ""
        if Settings.is_production:
            issued = self.address_provider.get_or_create_address(resolved_user_id, symbol)
            self._validate_deposit_address(symbol, issued.address)
            issued_normalized = self._normalize_address_for_chain(self._chain_family(symbol), issued.address)
            issued_fingerprint = self._address_fingerprint(self._chain_family(symbol), issued_normalized)

        managed_tx = not bool(getattr(self.conn, "in_transaction", False))
        if managed_tx:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute("SAVEPOINT deposit_route_issue")
        try:
            row = self.conn.execute("SELECT * FROM wallet_addresses WHERE user_id=? AND asset=?", (resolved_user_id, symbol)).fetchone()
            if row:
                stored_route = self._route_from_row(row)
                if Settings.is_production and issued is not None:
                    stored_origin = str(row["provider_origin"] or "")
                    stored_ref = str(row["provider_ref"] or "")
                    _, _, stored_fp = self._parsed_route_row(row)
                    if (
                        stored_route.address != issued_normalized
                        or stored_origin != issued.provider_origin
                        or stored_ref != issued.provider_ref
                        or stored_fp != issued_fingerprint
                    ):
                        LOGGER.error("Deposit route issuance conflict for user_id=%s asset=%s", resolved_user_id, symbol)
                        raise RuntimeError("address provider route conflict for existing deposit address")
                if managed_tx:
                    self.conn.commit()
                else:
                    self.conn.execute("RELEASE SAVEPOINT deposit_route_issue")
                return stored_route

            if Settings.is_production:
                assert issued is not None
                encrypted_addr = self._encrypt_field(issued_normalized)
                try:
                    self.conn.execute(
                        "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,derivation_index,destination_tag,derivation_path,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (resolved_user_id, symbol, self._chain_family(symbol), encrypted_addr, issued_fingerprint, None, None, None, issued.provider_origin, issued.provider_ref),
                    )
                except sqlite3.IntegrityError:
                    row = self.conn.execute("SELECT * FROM wallet_addresses WHERE user_id=? AND asset=?", (resolved_user_id, symbol)).fetchone()
                    if not row:
                        raise
                    stored_route = self._route_from_row(row)
                    stored_origin = str(row["provider_origin"] or "")
                    stored_ref = str(row["provider_ref"] or "")
                    if stored_route.address != issued_normalized or stored_origin != issued.provider_origin or stored_ref != issued.provider_ref:
                        LOGGER.error("Deposit route issuance conflict for user_id=%s asset=%s stored_address=%s issued_address=%s stored_origin=%s issued_origin=%s stored_ref=%s issued_ref=%s", resolved_user_id, symbol, stored_route.address, issued.address, stored_origin, issued.provider_origin, stored_ref, issued.provider_ref)
                        raise RuntimeError("address provider route conflict for existing deposit address")
                    if managed_tx:
                        self.conn.commit()
                    else:
                        self.conn.execute("RELEASE SAVEPOINT deposit_route_issue")
                    return stored_route
                if managed_tx:
                    self.conn.commit()
                else:
                    self.conn.execute("RELEASE SAVEPOINT deposit_route_issue")
                return DepositRoute(issued_normalized, symbol, self._chain_family(symbol), None, None)

            if symbol == "BTC":
                k = self.hd.derive_btc_address(resolved_user_id)
            elif symbol == "LTC":
                k = self.hd.derive_ltc_address(resolved_user_id)
            elif symbol in {"ETH", "USDT"}:
                k = self.hd.derive_eth_address(resolved_user_id)
            elif symbol == "SOL":
                k = self.hd.derive_sol_address(resolved_user_id)
            else:
                raise RuntimeError(f"unsupported production derivation for {symbol}")

            normalized_local = self._normalize_address_for_chain(self._chain_family(symbol), k.public_address)
            self.conn.execute(
                "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,derivation_index,destination_tag,derivation_path,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (resolved_user_id, symbol, self._chain_family(symbol), self._encrypt_field(normalized_local), self._address_fingerprint(self._chain_family(symbol), normalized_local), resolved_user_id, None, self._encrypt_field(k.path), "legacy_seed", f"user:{resolved_user_id}:{symbol}"),
            )
            if managed_tx:
                self.conn.commit()
            else:
                self.conn.execute("RELEASE SAVEPOINT deposit_route_issue")
            return DepositRoute(normalized_local, symbol, self._chain_family(symbol), None, k.path)
        except Exception:
            if managed_tx:
                self.conn.rollback()
            else:
                self.conn.execute("ROLLBACK TO SAVEPOINT deposit_route_issue")
                self.conn.execute("RELEASE SAVEPOINT deposit_route_issue")
            raise

    def credit_deposit_if_confirmed(self, user_id: int, asset: str, amount: Decimal, txid: str, unique_key: str, chain_family: str, confirmations: int, finalized: bool) -> bool:
        resolved_user_id = self._ensure_user_row(user_id)
        symbol = self._asset(asset)
        gross_amount = self._quantize_asset(symbol, Decimal(amount))
        fee_amount = self.deposit_platform_fee(symbol, gross_amount)
        net_amount = self.deposit_net_credit(symbol, gross_amount)

        # Wrap the entire read-modify-write in a single atomic transaction so
        # concurrent watcher threads cannot race and emit duplicate ledger credits.
        managed_tx = not bool(getattr(self.conn, "in_transaction", False))
        if managed_tx:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute("SAVEPOINT credit_deposit")
        try:
            row = self.conn.execute(
                "SELECT id,status,confirmations FROM deposits WHERE unique_key=?",
                (unique_key,),
            ).fetchone()
            if not row:
                cur = self.conn.execute(
                    "INSERT INTO deposits(user_id,asset,amount,txid,unique_key,chain_family,confirmations,status,credited_amount,platform_fee_amount) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (resolved_user_id, symbol, str(gross_amount), txid, unique_key, chain_family, confirmations, "credited" if finalized else "seen", str(net_amount), str(fee_amount)),
                )
                credited = False
                if finalized:
                    deposit_id = int(cur.lastrowid)
                    if net_amount > Decimal("0"):
                        self.ledger.add_entry("USER", resolved_user_id, resolved_user_id, symbol, net_amount, "DEPOSIT", "deposit", deposit_id)
                    if fee_amount > Decimal("0"):
                        self.ledger.add_entry("PLATFORM_REVENUE", None, None, symbol, fee_amount, "DEPOSIT_PLATFORM_FEE", "deposit", deposit_id)
                    credited = True
                if managed_tx:
                    self.conn.commit()
                else:
                    self.conn.execute("RELEASE SAVEPOINT credit_deposit")
                return credited

            deposit_id = int(row["id"])
            new_conf = max(int(row["confirmations"] or 0), int(confirmations))
            if str(row["status"] or "") == "credited":
                self.conn.execute(
                    "UPDATE deposits SET confirmations=?, txid=COALESCE(txid, ?), amount=?, credited_amount=?, platform_fee_amount=? WHERE id=?",
                    (new_conf, txid, str(gross_amount), str(net_amount), str(fee_amount), deposit_id),
                )
                if managed_tx:
                    self.conn.commit()
                else:
                    self.conn.execute("RELEASE SAVEPOINT credit_deposit")
                return False

            if finalized:
                self.conn.execute(
                    "UPDATE deposits SET confirmations=?, status='credited', txid=COALESCE(txid, ?), amount=?, credited_amount=?, platform_fee_amount=? WHERE id=?",
                    (new_conf, txid, str(gross_amount), str(net_amount), str(fee_amount), deposit_id),
                )
                if net_amount > Decimal("0"):
                    self.ledger.add_entry("USER", resolved_user_id, resolved_user_id, symbol, net_amount, "DEPOSIT", "deposit", deposit_id)
                if fee_amount > Decimal("0"):
                    self.ledger.add_entry("PLATFORM_REVENUE", None, None, symbol, fee_amount, "DEPOSIT_PLATFORM_FEE", "deposit", deposit_id)
                if managed_tx:
                    self.conn.commit()
                else:
                    self.conn.execute("RELEASE SAVEPOINT credit_deposit")
                return True

            self.conn.execute(
                "UPDATE deposits SET confirmations=?, txid=COALESCE(txid, ?), amount=?, credited_amount=?, platform_fee_amount=? WHERE id=?",
                (new_conf, txid, str(gross_amount), str(net_amount), str(fee_amount), deposit_id),
            )
            if managed_tx:
                self.conn.commit()
            else:
                self.conn.execute("RELEASE SAVEPOINT credit_deposit")
            return False
        except Exception:
            if managed_tx:
                self.conn.rollback()
            else:
                self.conn.execute("ROLLBACK TO SAVEPOINT credit_deposit")
                self.conn.execute("RELEASE SAVEPOINT credit_deposit")
            raise

    def total_balance(self, user_id: int, asset: str) -> Decimal:
        resolved_user_id = self._ensure_user_row(user_id)
        return self.ledger.total_balance(resolved_user_id, self._asset(asset))

    def locked_balance(self, user_id: int, asset: str) -> Decimal:
        resolved_user_id = self._ensure_user_row(user_id)
        return self.ledger.locked_balance(resolved_user_id, self._asset(asset))

    def available_balance(self, user_id: int, asset: str) -> Decimal:
        resolved_user_id = self._ensure_user_row(user_id)
        return self.ledger.available_balance(resolved_user_id, self._asset(asset))

    def lock_for_escrow(self, escrow_id: int, user_id: int, asset: str, amount: Decimal) -> None:
        resolved_user_id = self._ensure_user_row(user_id)
        if self.available_balance(resolved_user_id, asset) < Decimal(amount):
            raise ValueError("insufficient available balance")
        self.conn.execute("INSERT INTO escrow_locks(escrow_id,user_id,asset,amount,status) VALUES(?,?,?,?,?)", (escrow_id, resolved_user_id, self._asset(asset), str(Decimal(amount)), "locked"))
        self.ledger.add_entry("USER", resolved_user_id, resolved_user_id, asset, -Decimal(amount), "ESCROW_LOCK", "escrow", escrow_id)

    def release_escrow(self, escrow_id: int, seller_id: int, platform_fee: Decimal, bot_fee: Decimal, seller_payout: Decimal, bot_owner_id: int, asset: str) -> None:
        lock = self.conn.execute("SELECT * FROM escrow_locks WHERE escrow_id=?", (escrow_id,)).fetchone()
        if not lock or lock["status"] != "locked":
            raise ValueError("escrow lock missing")
        self.conn.execute("UPDATE escrow_locks SET status='released' WHERE escrow_id=?", (escrow_id,))
        self.ledger.add_entry("USER", seller_id, seller_id, asset, Decimal(seller_payout), "ESCROW_RELEASE", "escrow", escrow_id)
        self.ledger.add_entry("PLATFORM_REVENUE", None, None, asset, Decimal(platform_fee), "PLATFORM_FEE", "escrow", escrow_id)
        self.ledger.add_entry("BOT_OWNER_REVENUE", bot_owner_id, bot_owner_id, asset, Decimal(bot_fee), "BOT_FEE", "escrow", escrow_id)

    def cancel_escrow_lock(self, escrow_id: int) -> None:
        lock = self.conn.execute("SELECT * FROM escrow_locks WHERE escrow_id=?", (escrow_id,)).fetchone()
        if not lock or lock["status"] != "locked":
            raise ValueError("escrow lock missing")
        self.conn.execute("UPDATE escrow_locks SET status='released' WHERE escrow_id=?", (escrow_id,))
        self.ledger.add_entry("USER", int(lock["user_id"]), int(lock["user_id"]), lock["asset"], Decimal(lock["amount"]), "ESCROW_UNLOCK", "escrow", escrow_id)


    @staticmethod
    def _b58check_decode(value: str) -> bytes:
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        n = 0
        for ch in value:
            idx = alphabet.find(ch)
            if idx < 0:
                raise ValueError("invalid base58 character")
            n = n * 58 + idx
        data = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
        pad = len(value) - len(value.lstrip("1"))
        decoded = b"\x00" * pad + data
        if len(decoded) < 5:
            raise ValueError("base58 payload too short")
        payload, checksum = decoded[:-4], decoded[-4:]
        expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
        if checksum != expected:
            raise ValueError("invalid base58 checksum")
        return payload

    @staticmethod
    def _bech32_polymod(values: list[int]) -> int:
        generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
        chk = 1
        for value in values:
            top = chk >> 25
            chk = ((chk & 0x1FFFFFF) << 5) ^ value
            for i in range(5):
                if (top >> i) & 1:
                    chk ^= generator[i]
        return chk

    @classmethod
    def _bech32_hrp_expand(cls, hrp: str) -> list[int]:
        return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]

    @classmethod
    def _bech32_verify_checksum(cls, hrp: str, data: list[int]) -> bool:
        const = cls._bech32_polymod(cls._bech32_hrp_expand(hrp) + data)
        return const in {1, 0x2BC830A3}

    @classmethod
    def _bech32_decode(cls, addr: str) -> tuple[str, list[int]]:
        if addr.lower() != addr and addr.upper() != addr:
            raise ValueError("mixed-case bech32 address")
        addr = addr.lower()
        pos = addr.rfind("1")
        if pos < 1 or pos + 7 > len(addr):
            raise ValueError("invalid bech32 separator position")
        hrp = addr[:pos]
        data_part = addr[pos + 1:]
        charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
        data = []
        for ch in data_part:
            idx = charset.find(ch)
            if idx < 0:
                raise ValueError("invalid bech32 character")
            data.append(idx)
        if not cls._bech32_verify_checksum(hrp, data):
            raise ValueError("invalid bech32 checksum")
        return hrp, data[:-6]

    @classmethod
    def _validate_utxo_address(cls, asset: str, address: str) -> None:
        symbol = asset.upper()
        low = address.lower()
        if symbol == "BTC":
            if low.startswith("bc1"):
                hrp, data = cls._bech32_decode(address)
                if hrp != "bc" or not data:
                    raise ValueError("Invalid destination address for BTC")
                return
            payload = cls._b58check_decode(address)
            if payload[0] not in {0x00, 0x05}:
                raise ValueError("Invalid destination address for BTC")
            return
        if symbol == "LTC":
            if low.startswith("ltc1"):
                hrp, data = cls._bech32_decode(address)
                if hrp != "ltc" or not data:
                    raise ValueError("Invalid destination address for LTC")
                return
            payload = cls._b58check_decode(address)
            if payload[0] not in {0x30, 0x32, 0x05}:
                raise ValueError("Invalid destination address for LTC")
            return
        raise ValueError(f"unsupported UTXO asset: {symbol}")

    def _validate_withdrawal_address(self, asset: str, destination_address: str) -> None:
        symbol = self._asset(asset)
        address = (destination_address or "").strip()
        if not address:
            raise ValueError("destination address is required")

        if symbol in {"ETH", "USDT"}:
            try:
                from eth_utils import is_address, is_checksum_address
            except Exception as exc:
                raise RuntimeError("address validation dependencies are missing") from exc
            if not is_address(address):
                raise ValueError("Invalid destination address for ETH")
            if any(c.isalpha() for c in address[2:]) and not is_checksum_address(address):
                raise ValueError("Invalid checksum for Ethereum address")
            return

        if symbol == "SOL":
            try:
                from bip_utils import SolAddrDecoder
                SolAddrDecoder.DecodeAddr(address)
            except Exception as exc:
                raise ValueError("Invalid destination address for SOL") from exc
            return

        try:
            self._validate_utxo_address(symbol, address)
            return
        except ValueError:
            network = NETWORK_LABELS.get(symbol, symbol)
            raise ValueError(f"Invalid destination address for {network}")

    def validate_withdrawal_address(self, asset: str, destination_address: str) -> None:
        self._validate_withdrawal_address(asset, destination_address)

    def _withdrawn_usd_last_24h(self, user_id: int) -> Decimal:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self.conn.execute(
            "SELECT asset, amount FROM withdrawals WHERE user_id=? AND status IN ('pending','submitted','broadcasted','signer_retry') AND created_at >= ?",
            (user_id, cutoff),
        ).fetchall()
        total = Decimal("0")
        for row in rows:
            amount = Decimal(str(row["amount"] or "0"))
            try:
                total += self.price_service.get_usd_value(str(row["asset"]), amount)
            except Exception:
                total += self._fallback_price_service.get_usd_value(str(row["asset"]), amount)
        return total


    def _build_withdrawal_idempotency_key(self, withdrawal_id: int, user_id: int, asset: str, amount: Decimal, destination_address: str) -> str:
        payload = f"{int(withdrawal_id)}|{int(user_id)}|{asset.upper().strip()}|{str(Decimal(amount))}|{destination_address.strip()}".encode()
        digest = hashlib.sha256(payload).hexdigest()
        return f"wdrow:{int(withdrawal_id)}:{digest[:24]}"

    @staticmethod
    def _txid_sane_for_asset(asset: str, txid: str | None) -> bool:
        value = (txid or "").strip()
        if not value or len(value) < 16 or len(value) > 160:
            return False
        sym = asset.upper().strip()
        if sym in {"BTC", "LTC"}:
            low = value.lower()
            return len(low) == 64 and all(c in "0123456789abcdef" for c in low)
        if sym in {"ETH", "USDT"}:
            low = value.lower()
            return low.startswith("0x") and len(low) == 66 and all(c in "0123456789abcdef" for c in low[2:])
        return False

    def request_withdrawal(self, user_id: int, asset: str, amount: Decimal, destination_address: str):
        if not Settings.withdrawals_enabled:
            raise ValueError("withdrawals are temporarily unavailable")
        symbol = self._asset(asset)
        amt = Decimal(amount)
        if amt <= Decimal("0"):
            raise ValueError("amount must be positive")
        self._validate_withdrawal_address(symbol, destination_address)
        resolved_user_id = self._ensure_user_row(user_id)

        managed_tx = not bool(getattr(self.conn, "in_transaction", False))
        if managed_tx:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute("SAVEPOINT withdrawal_request")
        try:
            self._assert_not_frozen(resolved_user_id)
            min_interval = max(0, int(Settings.withdrawal_min_interval_seconds))
            if min_interval > 0:
                last = self.conn.execute(
                    "SELECT created_at FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 1",
                    (resolved_user_id,),
                ).fetchone()
                if last and last["created_at"]:
                    last_dt = datetime.strptime(last["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - last_dt).total_seconds() < min_interval:
                        raise ValueError("withdrawals are rate-limited; try again shortly")

            daily_limit = Decimal(Settings.withdrawal_daily_limit_usd)
            try:
                request_usd = self.price_service.get_usd_value(symbol, amt)
            except Exception:
                request_usd = self._fallback_price_service.get_usd_value(symbol, amt)
            minimum_usd = self.withdrawal_minimum_usd()
            if request_usd < minimum_usd:
                raise ValueError(f"minimum withdrawal is ${minimum_usd} USD equivalent")
            if self._withdrawn_usd_last_24h(resolved_user_id) + request_usd > daily_limit:
                raise ValueError("daily withdrawal limit exceeded")

            fee_amount = self.withdrawal_platform_fee(symbol, amt)
            total_debit = self.withdrawal_total_debit(symbol, amt)
            if self.ledger.available_balance(resolved_user_id, symbol) < total_debit:
                raise ValueError(f"insufficient available balance including the {Settings.withdrawal_platform_fee_percent}% withdrawal fee")
            encrypted_destination = self._encrypt_field(destination_address)

            cur = self.conn.execute(
                "INSERT INTO withdrawals(user_id,asset,amount,platform_fee_amount,destination_address,status,idempotency_key) VALUES(?,?,?,?,?,?,?)",
                (resolved_user_id, symbol, str(amt), str(fee_amount), encrypted_destination, "pending", ""),
            )
            wid = int(cur.lastrowid)
            # Bind a deterministic row-specific idempotency key once the row ID exists.
            # The update occurs inside the same transaction and the immutability trigger
            # allows empty->non-empty transitions while still blocking rebinding later.
            idempotency_key = self._build_withdrawal_idempotency_key(wid, resolved_user_id, symbol, amt, destination_address)
            self.conn.execute("UPDATE withdrawals SET idempotency_key=? WHERE id=?", (idempotency_key, wid))

            self.ledger.add_entry("USER", resolved_user_id, resolved_user_id, symbol, -total_debit, "WITHDRAWAL_RESERVE", "withdrawal", wid)
            if managed_tx:
                self.conn.commit()
            else:
                self.conn.execute("RELEASE SAVEPOINT withdrawal_request")
            return {"id": wid, "asset": symbol, "amount": amt, "platform_fee_amount": fee_amount, "total_debit": total_debit, "destination_address": destination_address}
        except Exception:
            if managed_tx:
                self.conn.rollback()
            else:
                self.conn.execute("ROLLBACK TO SAVEPOINT withdrawal_request")
                self.conn.execute("RELEASE SAVEPOINT withdrawal_request")
            raise

    def pending_withdrawals(self):
        rows = self.conn.execute("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id ASC").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["destination_address"] = self._decrypt_field(item.get("destination_address"))
            out.append(item)
        return out

    def signer_retry_withdrawals(self, limit: int = 20):
        rows = self.conn.execute(
            "SELECT id,user_id,asset,amount,destination_address,failure_reason,created_at FROM withdrawals WHERE status='signer_retry' ORDER BY created_at ASC, id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["destination_address"] = self._decrypt_field(item.get("destination_address"))
            out.append(item)
        return out

    def unresolved_withdrawals(self, limit: int = 50):
        rows = self.conn.execute(
            "SELECT id,user_id,asset,amount,destination_address,status,provider_ref,idempotency_key,external_status,failure_reason,created_at FROM withdrawals WHERE status IN ('pending','submitted','broadcasted','signer_retry') ORDER BY created_at ASC, id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["destination_address"] = self._decrypt_field(item.get("destination_address"))
            out.append(item)
        return out

    def unresolved_withdrawals_for_reconcile(self, limit: int = 100, submitted_after_s: int = 45, broadcasted_after_s: int = 120, signer_retry_after_s: int = 300):
        rows = self.conn.execute(
            """
            SELECT *
            FROM withdrawals
            WHERE (
                status='submitted' AND (
                    (last_reconciled_at IS NULL AND COALESCE(submitted_at, created_at) <= datetime('now', '-' || ? || ' seconds'))
                    OR
                    (last_reconciled_at IS NOT NULL AND last_reconciled_at <= datetime('now', '-' || ? || ' seconds'))
                )
            ) OR (
                status='broadcasted' AND (
                    (last_reconciled_at IS NULL AND COALESCE(broadcasted_at, submitted_at, created_at) <= datetime('now', '-' || ? || ' seconds'))
                    OR
                    (last_reconciled_at IS NOT NULL AND last_reconciled_at <= datetime('now', '-' || ? || ' seconds'))
                )
            ) OR (
                status='signer_retry' AND (
                    (last_reconciled_at IS NULL AND COALESCE(created_at, submitted_at) <= datetime('now', '-' || ? || ' seconds'))
                    OR
                    (last_reconciled_at IS NOT NULL AND last_reconciled_at <= datetime('now', '-' || ? || ' seconds'))
                )
            )
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (
                int(submitted_after_s), int(submitted_after_s),
                int(broadcasted_after_s), int(broadcasted_after_s),
                int(signer_retry_after_s), int(signer_retry_after_s),
                int(limit),
            ),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["destination_address"] = self._decrypt_field(item.get("destination_address"))
            out.append(item)
        return out

    def unresolved_withdrawal_for_reconcile_by_id(self, withdrawal_id: int, submitted_after_s: int = 45, broadcasted_after_s: int = 120, signer_retry_after_s: int = 300, ignore_backoff: bool = False):
        rows = self.conn.execute(
            """
            SELECT *
            FROM withdrawals
            WHERE id=?
              AND (
                    (? = 1 AND status IN ('submitted','broadcasted','signer_retry'))
                 OR (status='submitted' AND (
                        (last_reconciled_at IS NULL AND COALESCE(submitted_at, created_at) <= datetime('now', '-' || ? || ' seconds'))
                        OR
                        (last_reconciled_at IS NOT NULL AND last_reconciled_at <= datetime('now', '-' || ? || ' seconds'))
                    ))
                 OR (status='broadcasted' AND (
                        (last_reconciled_at IS NULL AND COALESCE(broadcasted_at, submitted_at, created_at) <= datetime('now', '-' || ? || ' seconds'))
                        OR
                        (last_reconciled_at IS NOT NULL AND last_reconciled_at <= datetime('now', '-' || ? || ' seconds'))
                    ))
                 OR (status='signer_retry' AND (
                        (last_reconciled_at IS NULL AND COALESCE(created_at, submitted_at) <= datetime('now', '-' || ? || ' seconds'))
                        OR
                        (last_reconciled_at IS NOT NULL AND last_reconciled_at <= datetime('now', '-' || ? || ' seconds'))
                    ))
              )
            LIMIT 1
            """,
            (
                int(withdrawal_id),
                1 if ignore_backoff else 0,
                int(submitted_after_s), int(submitted_after_s),
                int(broadcasted_after_s), int(broadcasted_after_s),
                int(signer_retry_after_s), int(signer_retry_after_s),
            ),
        ).fetchone()
        if not rows:
            return None
        item = dict(rows)
        item["destination_address"] = self._decrypt_field(item.get("destination_address"))
        return item

    def signer_retry_withdrawal(self, withdrawal_id: int):
        row = self.conn.execute(
            "SELECT id,user_id,asset,amount,destination_address,failure_reason,created_at,status FROM withdrawals WHERE id=?",
            (int(withdrawal_id),),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["destination_address"] = self._decrypt_field(item.get("destination_address"))
        return item

    def set_withdrawal_status(self, withdrawal_id: int, status: str, reason: str | None = None) -> None:
        row = self.conn.execute("SELECT * FROM withdrawals WHERE id=?", (int(withdrawal_id),)).fetchone()
        if not row:
            raise ValueError("withdrawal not found")
        if row["status"] != "signer_retry":
            raise ValueError("withdrawal is not in signer_retry")
        if status == "pending":
            self.conn.execute("UPDATE withdrawals SET status='pending', failure_reason=? WHERE id=?", ((reason or "requeued by admin")[:500], int(withdrawal_id)))
            return
        if status == "failed":
            self.mark_withdrawal_failed(int(withdrawal_id), reason or "marked failed by admin")
            return
        raise ValueError("unsupported status transition")

    def mark_withdrawal_broadcasted(self, withdrawal_id: int, txid: str, provider_ref: str | None = None, external_status: str | None = None, broadcasted_at: str | None = None) -> None:
        self._book_withdrawal_platform_fee_if_needed(int(withdrawal_id))
        self.conn.execute(
            "UPDATE withdrawals SET status='broadcasted', txid=?, provider_ref=COALESCE(provider_ref,?), external_status=COALESCE(?, external_status), broadcasted_at=COALESCE(?,broadcasted_at,CURRENT_TIMESTAMP), last_reconciled_at=NULL WHERE id=?",
            (txid, provider_ref, (external_status or 'broadcasted')[:64], broadcasted_at, withdrawal_id),
        )

    def mark_withdrawal_submitted(self, withdrawal_id: int, provider_ref: str | None, external_status: str | None, submitted_at: str | None) -> None:
        self.conn.execute(
            "UPDATE withdrawals SET status='submitted', external_status=?, submitted_at=COALESCE(?,submitted_at,CURRENT_TIMESTAMP), provider_ref=COALESCE(provider_ref,?), last_reconciled_at=NULL WHERE id=?",
            ((external_status or "submitted")[:64], submitted_at, provider_ref, int(withdrawal_id)),
        )

    def mark_withdrawal_confirmed(self, withdrawal_id: int, txid: str, provider_ref: str | None, external_status: str | None) -> None:
        self._book_withdrawal_platform_fee_if_needed(int(withdrawal_id))
        self.conn.execute(
            "UPDATE withdrawals SET status='confirmed', txid=?, provider_ref=COALESCE(provider_ref,?), external_status=?, broadcasted_at=COALESCE(broadcasted_at,CURRENT_TIMESTAMP), last_reconciled_at=CURRENT_TIMESTAMP WHERE id=?",
            (txid, provider_ref, (external_status or "confirmed")[:64], int(withdrawal_id)),
        )

    def mark_withdrawal_reconciled(self, withdrawal_id: int) -> None:
        self.conn.execute("UPDATE withdrawals SET last_reconciled_at=CURRENT_TIMESTAMP WHERE id=?", (int(withdrawal_id),))

    def mark_withdrawal_signer_retry(self, withdrawal_id: int, reason: str) -> None:
        row = self.conn.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
        if not row:
            return
        self.conn.execute(
            "UPDATE withdrawals SET status=?, txid=NULL, external_status='ambiguous', failure_reason=?, last_reconciled_at=CURRENT_TIMESTAMP WHERE id=?",
            ("signer_retry", sanitize_runtime_error(reason, max_len=220), withdrawal_id),
        )

    def mark_withdrawal_failed(self, withdrawal_id: int, reason: str) -> None:
        row = self.conn.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
        if not row:
            return
        self.conn.execute(
            "UPDATE withdrawals SET status=?, txid=NULL, external_status='failed', failure_reason=? WHERE id=?",
            ("failed", sanitize_runtime_error(reason, max_len=220), withdrawal_id),
        )
        release_total = Decimal(str(row["amount"] or "0")) + Decimal(str(row["platform_fee_amount"] if "platform_fee_amount" in row.keys() else None or "0"))
        self.ledger.add_entry("USER", row["user_id"], row["user_id"], row["asset"], release_total, "WITHDRAWAL_RELEASE", "withdrawal", withdrawal_id)

    def persist_withdrawal_idempotency(self, withdrawal_id: int, idempotency_key: str) -> None:
        row = self.conn.execute("SELECT idempotency_key FROM withdrawals WHERE id=?", (int(withdrawal_id),)).fetchone()
        if not row:
            raise RuntimeError("withdrawal not found")
        existing = str(row["idempotency_key"] or "").strip()
        incoming = str(idempotency_key or "").strip()
        if existing and incoming and existing != incoming:
            raise RuntimeError("idempotency_key mismatch")
        self.conn.execute("UPDATE withdrawals SET idempotency_key=COALESCE(NULLIF(idempotency_key,''), ?) WHERE id=?", (incoming, int(withdrawal_id)))

    def record_withdrawal_provider_result(self, withdrawal_id: int, provider_origin: str, idempotency_key: str, result) -> None:
        row = self.conn.execute("SELECT provider_origin, provider_ref, idempotency_key, asset FROM withdrawals WHERE id=?", (int(withdrawal_id),)).fetchone()
        if not row:
            return
        existing_origin = str(row["provider_origin"] or "").strip()
        if existing_origin and existing_origin != provider_origin:
            raise RuntimeError("provider_origin mismatch")
        existing_idem = str(row["idempotency_key"] or "").strip()
        incoming_idem = str(idempotency_key or "").strip()
        if existing_idem and incoming_idem and existing_idem != incoming_idem:
            raise RuntimeError("idempotency_key mismatch")
        existing_ref = str(row["provider_ref"] or "").strip()
        new_ref = str(getattr(result, "provider_ref", "") or "").strip()
        if existing_ref and new_ref and existing_ref != new_ref:
            raise RuntimeError("provider_ref mismatch")
        result_asset = str(getattr(result, "asset", "") or "").strip().upper()
        if result_asset and result_asset != str(row["asset"] or "").strip().upper():
            raise RuntimeError("provider asset mismatch")
        metadata = getattr(result, "metadata", None)
        metadata_json = None
        if metadata is not None:
            if not isinstance(metadata, dict):
                # WARNING: malformed provider metadata is rejected to fail closed and avoid persisting untrusted payloads.
                raise RuntimeError("provider metadata must be an object")
            metadata_json_candidate = json.dumps(metadata, sort_keys=True)
            if len(metadata_json_candidate) > 2000:
                # WARNING: oversized provider metadata is rejected to avoid database/log payload abuse.
                raise RuntimeError("provider metadata too large")
            metadata_json = metadata_json_candidate
        status = str(getattr(result, "status", "") or "").strip().lower()
        txid = str(getattr(result, "txid", "") or "").strip() or None
        if status in {"broadcasted", "confirmed"} and not self._txid_sane_for_asset(str(row["asset"]), txid):
            # WARNING: malformed txid is rejected and retried to avoid persisting forged or unusable on-chain references.
            raise RuntimeError("malformed txid for on-chain status")
        self.conn.execute(
            "UPDATE withdrawals SET provider_origin=COALESCE(provider_origin, ?), provider_ref=COALESCE(provider_ref, NULLIF(?,'')), idempotency_key=COALESCE(NULLIF(idempotency_key,''), ?), external_status=?, submitted_at=COALESCE(submitted_at, ?), broadcasted_at=COALESCE(broadcasted_at, ?), tx_metadata_json=COALESCE(?, tx_metadata_json) WHERE id=?",
            (
                provider_origin,
                new_ref,
                idempotency_key,
                sanitize_runtime_error(str(getattr(result, "external_status", "") or getattr(result, "status", "") or ""), max_len=64) or None,
                getattr(result, "submitted_at", None),
                getattr(result, "broadcasted_at", None),
                metadata_json,
                int(withdrawal_id),
            ),
        )

    def platform_revenue_balances(self) -> dict[str, Decimal]:
        balances: dict[str, Decimal] = {}
        for asset in Settings.supported_assets:
            balances[asset] = self.account_revenue_balance("PLATFORM_REVENUE", None, asset)
        return balances

    def account_revenue_balance(self, account_type: str, owner_id: int | None, asset: str) -> Decimal:
        symbol = self._asset(asset)
        if account_type == "BOT_OWNER_REVENUE":
            rows = self.conn.execute(
                "SELECT amount FROM ledger_entries WHERE account_type=? AND asset=? AND account_owner_id=?",
                (account_type, symbol, owner_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT amount FROM ledger_entries WHERE account_type=? AND asset=?",
                (account_type, symbol),
            ).fetchall()
        total = Decimal("0")
        for row in rows:
            total += Decimal(str(row["amount"] or "0"))
        return total

    def withdrawal_history(self, user_id: int, page: int = 1, per_page: int = 10):
        resolved_user_id = self._ensure_user_row(user_id)
        page = max(1, int(page))
        per_page = max(1, int(per_page))
        total = self.conn.execute("SELECT COUNT(*) c FROM withdrawals WHERE user_id=?", (resolved_user_id,)).fetchone()["c"]
        pages = max(1, (int(total) + per_page - 1) // per_page)
        page = min(page, pages)
        offset = (page - 1) * per_page
        rows = self.conn.execute(
            "SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (resolved_user_id, per_page, offset),
        ).fetchall()
        return rows, page, pages
