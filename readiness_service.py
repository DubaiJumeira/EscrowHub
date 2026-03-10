from __future__ import annotations

from dataclasses import dataclass
import os

from config.settings import Settings
from error_sanitizer import sanitize_runtime_error
from infra.db.database import get_connection, init_db
from runtime_preflight import FatalStartupError, PreflightIntegrityError, PreflightStatus, run_startup_preflight
from signer.signer_service import SignerService
from wallet_service import WalletService


READINESS_READY = "READY"
READINESS_DEGRADED = "DEGRADED"
READINESS_BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ReadinessReport:
    status: str
    blocked_reasons: tuple[str, ...] = ()
    degraded_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    checks: tuple[tuple[str, str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.status != READINESS_BLOCKED


def _safe_reason(raw: object) -> str:
    return sanitize_runtime_error(raw, max_len=160)


def _check_required_env_vars() -> list[str]:
    missing: list[str] = []
    required = ["APP_ENV", "SQLITE_DB_PATH", "TELEGRAM_BOT_TOKEN", "ADDRESS_PROVIDER", "WITHDRAWAL_PROVIDER"]
    for key in required:
        if not os.getenv(key, "").strip():
            missing.append(f"{key} is missing")

    # WARNING: production readiness fails closed when provider credentials/URLs are absent.
    # Secure alternative: inject secrets via deployment secret manager and enforce HTTPS endpoints.
    if Settings.is_production:
        if os.getenv("ADDRESS_PROVIDER", "").strip().lower() != "http":
            missing.append("ADDRESS_PROVIDER must be 'http' in production")
        if not os.getenv("ADDRESS_PROVIDER_URL", "").strip():
            missing.append("ADDRESS_PROVIDER_URL is missing")
        if not os.getenv("ADDRESS_PROVIDER_TOKEN", "").strip():
            missing.append("ADDRESS_PROVIDER_TOKEN is missing")
        if os.getenv("WITHDRAWAL_PROVIDER", os.getenv("SIGNER_PROVIDER", "")).strip().lower() != "http":
            missing.append("WITHDRAWAL_PROVIDER must be 'http' in production")
        if not os.getenv("WITHDRAWAL_PROVIDER_URL", "").strip():
            missing.append("WITHDRAWAL_PROVIDER_URL is missing")
        if not os.getenv("WITHDRAWAL_PROVIDER_TOKEN", "").strip():
            missing.append("WITHDRAWAL_PROVIDER_TOKEN is missing")
    return missing


def _run_preflight(name: str) -> tuple[PreflightStatus | None, str | None]:
    try:
        return run_startup_preflight(name), None
    except (PreflightIntegrityError, FatalStartupError) as exc:
        return None, _safe_reason(exc)


def assess_release_readiness(allow_degraded: bool = False) -> ReadinessReport:
    blocked: list[str] = []
    degraded: list[str] = []
    warnings: list[str] = []
    checks: list[tuple[str, str, str]] = []

    try:
        conn = get_connection()
        init_db(conn)
        checks.append(("db_connectivity", "pass", "ok"))
    except Exception as exc:
        # WARNING: DB/bootstrap failures are startup-fatal and intentionally block launch to fail closed.
        blocked.append(f"db/schema check failed: {_safe_reason(exc)}")
        checks.append(("db_connectivity", "fail", _safe_reason(exc)))
        conn = None

    bot_status, bot_error = _run_preflight("bot")
    if bot_status is None:
        blocked.append(f"bot startup preflight failed: {bot_error or 'unknown'}")
        checks.append(("route_integrity", "fail", bot_error or "preflight failed"))
    else:
        checks.append(("route_integrity", "pass" if bot_status.route_integrity_ready else "fail", "ok" if bot_status.route_integrity_ready else "route integrity unavailable"))

    signer_status, signer_error = _run_preflight("signer")
    if signer_status is None:
        blocked.append(f"signer startup preflight failed: {signer_error or 'unknown'}")
        checks.append(("startup_fatal_conditions", "fail", signer_error or "signer preflight failed"))
    else:
        checks.append(("startup_fatal_conditions", "pass", "ok"))

    watcher_status, watcher_error = _run_preflight("watcher")
    if watcher_status is None:
        blocked.append(f"watcher startup preflight failed: {watcher_error or 'unknown'}")

    env_issues = _check_required_env_vars()
    if env_issues:
        blocked.extend(env_issues)
        checks.append(("required_env_vars", "fail", "; ".join(env_issues)))
    else:
        checks.append(("required_env_vars", "pass", "ok"))

    if conn is not None:
        try:
            wallet = WalletService(conn)
            dep_ready, dep_reason = wallet.address_provider.is_ready()
            signer = SignerService()
            wd_ready, wd_reason = signer.readiness()
            checks.append(("deposit_provider", "pass" if dep_ready else "fail", _safe_reason(dep_reason or "ok")))
            checks.append(("withdrawal_provider", "pass" if wd_ready else "fail", _safe_reason(wd_reason or "ok")))
            checks.append(("signer_readiness", "pass" if wd_ready else "fail", _safe_reason(wd_reason or "ok")))
            if not dep_ready:
                blocked.append(f"deposit provider unavailable: {_safe_reason(dep_reason or 'not ready')}")
            if not wd_ready:
                blocked.append(f"withdrawal provider/signer unavailable: {_safe_reason(wd_reason or 'not ready')}")
        except Exception as exc:
            blocked.append(f"provider readiness check failed: {_safe_reason(exc)}")
            checks.append(("provider_readiness", "fail", _safe_reason(exc)))
        finally:
            conn.close()

    sqlite_path = os.getenv("SQLITE_DB_PATH", "").strip() or ":memory:"
    if sqlite_path != ":memory:":
        warnings.append("single-node SQLite posture active (not multi-node)")
    checks.append(("sqlite_posture", "warn", "single-node SQLite only"))

    if warnings and not blocked:
        degraded.extend(warnings)

    if watcher_status is not None and watcher_status.reasons:
        degraded.append("watcher startup preflight reported degraded reasons")

    if blocked:
        status = READINESS_BLOCKED
    elif degraded:
        # WARNING: allow_degraded intentionally affects exit policy only, never the reported readiness truth state.
        # Secure alternative: keep state truthful (DEGRADED) and let entrypoint decide process exit behavior.
        status = READINESS_DEGRADED
    else:
        status = READINESS_READY

    return ReadinessReport(
        status=status,
        blocked_reasons=tuple(blocked),
        degraded_reasons=tuple(degraded),
        warnings=tuple(warnings),
        checks=tuple(checks),
    )
