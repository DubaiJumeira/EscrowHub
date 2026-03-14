from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SUPPORTED_ASSETS = ("BTC", "LTC", "ETH", "USDT", "SOL")
SUCCESS_STATUSES = {"submitted", "broadcasted", "confirmed"}
FAILURE_STATUSES = {"rejected", "permanent_failure", "retryable", "ambiguous", "unknown"}
ALL_STATUSES = SUCCESS_STATUSES | FAILURE_STATUSES


@dataclass(frozen=True)
class ProviderRecord:
    withdrawal_id: int
    user_id: int
    asset: str
    amount: str
    destination_address: str
    idempotency_key: str
    provider_ref: str
    status: str
    external_status: str | None
    txid: str | None
    message: str | None
    submitted_at: str | None
    broadcasted_at: str | None
    metadata: dict[str, Any] | None


class ValidationError(Exception):
    pass


class ProviderStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_withdrawals (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  withdrawal_id INTEGER NOT NULL,
                  user_id INTEGER NOT NULL,
                  asset TEXT NOT NULL,
                  amount TEXT NOT NULL,
                  destination_address TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  provider_ref TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL,
                  external_status TEXT,
                  txid TEXT,
                  message TEXT,
                  submitted_at TEXT,
                  broadcasted_at TEXT,
                  metadata_json TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_provider_withdrawals_ref ON provider_withdrawals(provider_ref);
                CREATE INDEX IF NOT EXISTS idx_provider_withdrawals_status ON provider_withdrawals(status, created_at);
                CREATE TABLE IF NOT EXISTS provider_audit_log (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  provider_ref TEXT NOT NULL,
                  action TEXT NOT NULL,
                  payload_json TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    def get_by_idempotency(self, idempotency_key: str) -> ProviderRecord | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM provider_withdrawals WHERE idempotency_key=? LIMIT 1", (idempotency_key,)).fetchone()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def get_by_provider_ref(self, provider_ref: str) -> ProviderRecord | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM provider_withdrawals WHERE provider_ref=? LIMIT 1", (provider_ref,)).fetchone()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def create_submitted(self, *, withdrawal_id: int, user_id: int, asset: str, amount: str, destination_address: str, idempotency_key: str) -> ProviderRecord:
        provider_ref = self._provider_ref(withdrawal_id, idempotency_key)
        submitted_at = _utc_now()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO provider_withdrawals(
                    withdrawal_id,user_id,asset,amount,destination_address,
                    idempotency_key,provider_ref,status,external_status,message,submitted_at,metadata_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(withdrawal_id),
                    int(user_id),
                    asset,
                    amount,
                    destination_address,
                    idempotency_key,
                    provider_ref,
                    "submitted",
                    "queued_manual_review",
                    "queued for manual withdrawal execution",
                    submitted_at,
                    json.dumps({"mode": "manual"}, sort_keys=True),
                    submitted_at,
                ),
            )
            conn.execute(
                "INSERT INTO provider_audit_log(provider_ref,action,payload_json) VALUES(?,?,?)",
                (
                    provider_ref,
                    "execute",
                    json.dumps(
                        {
                            "withdrawal_id": int(withdrawal_id),
                            "user_id": int(user_id),
                            "asset": asset,
                            "amount": amount,
                            "destination_address": destination_address,
                            "idempotency_key": idempotency_key,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        record = self.get_by_provider_ref(provider_ref)
        if record is None:
            raise RuntimeError("failed to persist provider record")
        return record

    def update_status(
        self,
        provider_ref: str,
        *,
        status: str,
        txid: str | None = None,
        message: str | None = None,
        external_status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProviderRecord:
        if status not in ALL_STATUSES:
            raise ValidationError(f"unsupported provider status: {status}")
        if status in {"broadcasted", "confirmed"} and not txid:
            raise ValidationError("txid is required for broadcasted or confirmed status")
        row = self.get_by_provider_ref(provider_ref)
        if row is None:
            raise ValidationError("provider_ref not found")
        if txid is not None and not _txid_sane_for_asset(row.asset, txid):
            raise ValidationError(f"malformed txid for {row.asset}")
        now = _utc_now()
        submitted_at = row.submitted_at or now
        broadcasted_at = row.broadcasted_at or (now if status in {"broadcasted", "confirmed"} else None)
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE provider_withdrawals
                SET status=?,
                    external_status=?,
                    txid=COALESCE(?, txid),
                    message=?,
                    submitted_at=COALESCE(submitted_at, ?),
                    broadcasted_at=COALESCE(?, broadcasted_at),
                    metadata_json=COALESCE(?, metadata_json),
                    updated_at=?
                WHERE provider_ref=?
                """,
                (
                    status,
                    (external_status or status)[:64],
                    txid,
                    (message or "")[:500] or None,
                    submitted_at,
                    broadcasted_at,
                    json.dumps(metadata, sort_keys=True) if metadata is not None else None,
                    now,
                    provider_ref,
                ),
            )
            conn.execute(
                "INSERT INTO provider_audit_log(provider_ref,action,payload_json) VALUES(?,?,?)",
                (
                    provider_ref,
                    f"status:{status}",
                    json.dumps(
                        {
                            "txid": txid,
                            "message": message,
                            "external_status": external_status,
                            "metadata": metadata,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        updated = self.get_by_provider_ref(provider_ref)
        if updated is None:
            raise RuntimeError("provider_ref disappeared after update")
        return updated

    def list_records(self, status: str | None = None, limit: int = 20) -> list[ProviderRecord]:
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM provider_withdrawals WHERE status=? ORDER BY created_at ASC, id ASC LIMIT ?",
                    (status, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM provider_withdrawals ORDER BY created_at ASC, id ASC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [self._row_to_record(row) for row in rows]
        finally:
            conn.close()

    @staticmethod
    def _provider_ref(withdrawal_id: int, idempotency_key: str) -> str:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:20]
        return f"mprov:{int(withdrawal_id)}:{digest}"

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ProviderRecord:
        metadata = None
        if row["metadata_json"]:
            try:
                payload = json.loads(str(row["metadata_json"]))
                if isinstance(payload, dict):
                    metadata = payload
            except Exception:
                metadata = None
        return ProviderRecord(
            withdrawal_id=int(row["withdrawal_id"]),
            user_id=int(row["user_id"]),
            asset=str(row["asset"]),
            amount=str(row["amount"]),
            destination_address=str(row["destination_address"]),
            idempotency_key=str(row["idempotency_key"]),
            provider_ref=str(row["provider_ref"]),
            status=str(row["status"]),
            external_status=str(row["external_status"] or "") or None,
            txid=str(row["txid"] or "") or None,
            message=str(row["message"] or "") or None,
            submitted_at=str(row["submitted_at"] or "") or None,
            broadcasted_at=str(row["broadcasted_at"] or "") or None,
            metadata=metadata,
        )


class WithdrawalProviderApplication:
    def __init__(self, store: ProviderStore, auth_token: str | None = None) -> None:
        self.store = store
        self.auth_token = (auth_token or "").strip() or None

    def handle_health(self) -> tuple[int, dict[str, Any]]:
        return HTTPStatus.OK, {
            "status": "ok",
            "mode": "manual_queue",
            "supported_assets": list(SUPPORTED_ASSETS),
        }

    def handle_execute(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            req = _validate_execute_payload(payload)
        except ValidationError as exc:
            return HTTPStatus.OK, {
                "status": "rejected",
                "asset": str(payload.get("asset") or "").upper() or None,
                "message": str(exc)[:500],
            }
        existing = self.store.get_by_idempotency(req["idempotency_key"])
        if existing is not None:
            if not _record_matches(existing, req):
                return HTTPStatus.OK, {
                    "status": "ambiguous",
                    "provider_ref": existing.provider_ref,
                    "asset": existing.asset,
                    "external_status": existing.external_status or existing.status,
                    "message": "idempotency_key already exists with different withdrawal fields",
                    "submitted_at": existing.submitted_at,
                    "broadcasted_at": existing.broadcasted_at,
                    "txid": existing.txid,
                    "metadata": existing.metadata,
                }
            return HTTPStatus.OK, _record_to_response(existing)
        record = self.store.create_submitted(**req)
        return HTTPStatus.OK, _record_to_response(record)

    def handle_reconcile(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            req = _validate_reconcile_payload(payload)
        except ValidationError as exc:
            return HTTPStatus.OK, {"status": "unknown", "message": str(exc)[:500]}
        record = None
        if req["provider_ref"]:
            record = self.store.get_by_provider_ref(req["provider_ref"])
            if record is not None and record.idempotency_key != req["idempotency_key"]:
                return HTTPStatus.OK, {
                    "status": "ambiguous",
                    "provider_ref": record.provider_ref,
                    "asset": record.asset,
                    "external_status": record.external_status or record.status,
                    "message": "provider_ref/idempotency_key mismatch",
                }
        if record is None:
            record = self.store.get_by_idempotency(req["idempotency_key"])
        if record is None:
            return HTTPStatus.OK, {"status": "unknown", "message": "provider record not found"}
        return HTTPStatus.OK, _record_to_response(record)


def build_handler(app: WithdrawalProviderApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EscrowHubWithdrawalProvider/1.0"

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def _auth_ok(self) -> bool:
            if not app.auth_token:
                return True
            auth = str(self.headers.get("Authorization") or "").strip()
            return auth == f"Bearer {app.auth_token}"

        def _read_json(self) -> dict[str, Any]:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode() or "{}")
            except Exception as exc:
                raise ValidationError("malformed JSON body") from exc
            if not isinstance(payload, dict):
                raise ValidationError("JSON body must be an object")
            return payload

        def _write_json(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, sort_keys=True).encode()
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if not self._auth_ok():
                self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if self.path == "/health":
                status, body = app.handle_health()
                self._write_json(status, body)
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._auth_ok():
                self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                payload = self._read_json()
            except ValidationError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if self.path == "/v1/withdrawals":
                status, body = app.handle_execute(payload)
                self._write_json(status, body)
                return
            if self.path == "/v1/withdrawals/reconcile":
                status, body = app.handle_reconcile(payload)
                self._write_json(status, body)
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler


def run_server() -> None:
    host = os.getenv("WITHDRAWAL_PROVIDER_BIND", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("WITHDRAWAL_PROVIDER_PORT", "8787"))
    db_path = os.getenv("WITHDRAWAL_PROVIDER_DB_PATH", "/var/lib/escrowhub-withdrawal-provider/provider.db").strip() or "provider.db"
    auth_token = os.getenv("WITHDRAWAL_PROVIDER_TOKEN", "").strip()
    store = ProviderStore(db_path)
    app = WithdrawalProviderApplication(store, auth_token=auth_token)
    server = ThreadingHTTPServer((host, port), build_handler(app))
    print(f"withdrawal provider listening on {host}:{port} db={db_path}")
    server.serve_forever()


class _B58Decode:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

    @classmethod
    def decode_raw(cls, raw: str, *, error_message: str = "invalid base58 value") -> bytes:
        num = 0
        for ch in raw:
            idx = cls.alphabet.find(ch)
            if idx < 0:
                raise ValidationError(error_message)
            num = num * 58 + idx
        out = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
        pad = len(raw) - len(raw.lstrip("1"))
        return b"\x00" * pad + out

    @classmethod
    def decode(cls, raw: str) -> bytes:
        full = cls.decode_raw(raw, error_message="invalid base58 address")
        if len(full) < 4:
            raise ValidationError("base58 payload too short")
        payload, checksum = full[:-4], full[-4:]
        if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
            raise ValidationError("invalid base58 checksum")
        return payload


def _bech32_polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= generator[i]
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_decode(addr: str) -> tuple[str, list[int]]:
    if addr.lower() != addr and addr.upper() != addr:
        raise ValidationError("mixed-case bech32 address")
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr):
        raise ValidationError("invalid bech32 separator position")
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    data_part = addr[pos + 1 :]
    data: list[int] = []
    for ch in data_part:
        idx = charset.find(ch)
        if idx < 0:
            raise ValidationError("invalid bech32 character")
        data.append(idx)
    const = _bech32_polymod(_bech32_hrp_expand(addr[:pos]) + data)
    if const not in {1, 0x2BC830A3}:
        raise ValidationError("invalid bech32 checksum")
    return addr[:pos], data[:-6]


def _validate_utxo_address(asset: str, address: str) -> None:
    low = address.lower()
    if asset == "BTC":
        if low.startswith("bc1"):
            hrp, data = _bech32_decode(address)
            if hrp != "bc" or not data:
                raise ValidationError("invalid BTC bech32 address")
            return
        payload = _B58Decode.decode(address)
        if payload[0] not in {0x00, 0x05}:
            raise ValidationError("invalid BTC address version")
        return
    if asset == "LTC":
        if low.startswith("ltc1"):
            hrp, data = _bech32_decode(address)
            if hrp != "ltc" or not data:
                raise ValidationError("invalid LTC bech32 address")
            return
        payload = _B58Decode.decode(address)
        if payload[0] not in {0x30, 0x32, 0x05}:
            raise ValidationError("invalid LTC address version")
        return
    raise ValidationError(f"unsupported UTXO asset: {asset}")


def _validate_destination_address(asset: str, address: str) -> None:
    address = (address or "").strip()
    if not address:
        raise ValidationError("destination_address is required")
    if asset in {"ETH", "USDT"}:
        try:
            from eth_utils import is_address, is_checksum_address
        except Exception as exc:
            raise ValidationError("eth_utils is required for Ethereum address validation") from exc
        if not is_address(address):
            raise ValidationError("invalid destination address for Ethereum")
        if any(c.isalpha() for c in address[2:]) and not is_checksum_address(address):
            raise ValidationError("invalid checksum for Ethereum address")
        return
    if asset == "SOL":
        raw = _B58Decode.decode_raw(address, error_message="invalid base58 address")
        if len(raw) != 32:
            raise ValidationError("invalid destination address for SOL")
        return
    _validate_utxo_address(asset, address)


def _record_matches(record: ProviderRecord, request_payload: dict[str, Any]) -> bool:
    return (
        record.withdrawal_id == int(request_payload["withdrawal_id"])
        and record.user_id == int(request_payload["user_id"])
        and record.asset == str(request_payload["asset"])
        and record.amount == str(request_payload["amount"])
        and record.destination_address == str(request_payload["destination_address"])
    )


def _validate_execute_payload(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        withdrawal_id = int(payload.get("withdrawal_id"))
        user_id = int(payload.get("user_id"))
    except Exception as exc:
        raise ValidationError("withdrawal_id and user_id must be integers") from exc
    asset = str(payload.get("asset") or "").upper().strip()
    if asset not in SUPPORTED_ASSETS:
        raise ValidationError(f"unsupported asset: {asset or 'empty'}")
    try:
        amount = Decimal(str(payload.get("amount") or "0"))
    except Exception as exc:
        raise ValidationError("amount must be a decimal string") from exc
    if amount <= Decimal("0"):
        raise ValidationError("amount must be positive")
    destination_address = str(payload.get("destination_address") or "").strip()
    _validate_destination_address(asset, destination_address)
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if not idempotency_key:
        raise ValidationError("idempotency_key is required")
    return {
        "withdrawal_id": withdrawal_id,
        "user_id": user_id,
        "asset": asset,
        "amount": format(amount, "f"),
        "destination_address": destination_address,
        "idempotency_key": idempotency_key,
    }


def _validate_reconcile_payload(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        withdrawal_id = int(payload.get("withdrawal_id"))
    except Exception as exc:
        raise ValidationError("withdrawal_id must be an integer") from exc
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if not idempotency_key:
        raise ValidationError("idempotency_key is required")
    provider_ref = str(payload.get("provider_ref") or "").strip() or None
    return {
        "withdrawal_id": withdrawal_id,
        "idempotency_key": idempotency_key,
        "provider_ref": provider_ref,
    }


def _record_to_response(record: ProviderRecord) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": record.status,
        "asset": record.asset,
        "provider_ref": record.provider_ref,
        "external_status": record.external_status or record.status,
        "message": record.message,
        "submitted_at": record.submitted_at,
        "broadcasted_at": record.broadcasted_at,
        "metadata": record.metadata,
    }
    if record.txid:
        body["txid"] = record.txid
    return {k: v for k, v in body.items() if v is not None}


def _txid_sane_for_asset(asset: str, txid: str | None) -> bool:
    value = str(txid or "").strip()
    if not value or len(value) < 16 or len(value) > 160:
        return False
    if asset in {"BTC", "LTC"}:
        low = value.lower()
        return len(low) == 64 and all(c in "0123456789abcdef" for c in low)
    if asset in {"ETH", "USDT"}:
        low = value.lower()
        return low.startswith("0x") and len(low) == 66 and all(c in "0123456789abcdef" for c in low[2:])
    if asset == "SOL":
        try:
            return len(_B58Decode.decode_raw(value, error_message="invalid base58 signature")) == 64
        except ValidationError:
            return False
    return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
