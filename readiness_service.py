from __future__ import annotations

from dataclasses import dataclass
import os

from config.settings import Settings
from error_sanitizer import sanitize_runtime_error
from infra.db.database import get_connection, init_db
from runtime_preflight import FatalStartupError, PreflightIntegrityError, PreflightStatus, run_startup_preflight
from signer.signer_service import SignerService
from wallet_service import WalletService
from watcher_status_service import env_flag_enabled


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


def _effective_sqlite_path() -> str:
    return (os.getenv("SQLITE_DB_PATH") or os.getenv("DATABASE_URL") or "").strip()


def _effective_address_provider_mode() -> str:
    configured = (os.getenv("ADDRESS_PROVIDER") or "auto").strip().lower()
    if configured in {"", "auto", "default"}:
        if os.getenv("ADDRESS_PROVIDER_URL", "").strip():
            return "http"
        if os.getenv("HD_WALLET_SEED_HEX", "").strip():
            return "local_hd"
        return "disabled"
    return configured


def _withdrawals_enabled() -> bool:
    return str(os.getenv("WITHDRAWALS_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}


def _check_required_env_vars() -> list[str]:
    missing: list[str] = []
    required = ["TELEGRAM_BOT_TOKEN"]
    for key in required:
        if not os.getenv(key, "").strip():
            missing.append(f"{key} is missing")

    if not os.getenv("APP_ENV", "").strip():
        missing.append("APP_ENV is missing")

    if not _effective_sqlite_path():
        missing.append("SQLITE_DB_PATH or DATABASE_URL is missing")

    if Settings.is_production and not os.getenv("ENCRYPTION_KEY", "").strip():
        missing.append("ENCRYPTION_KEY is missing")

    provider_mode = _effective_address_provider_mode()
    if provider_mode == "disabled":
        missing.append("deposit address provider is not configured")
    elif provider_mode == "http":
        if not os.getenv("ADDRESS_PROVIDER_URL", "").strip():
            missing.append("ADDRESS_PROVIDER_URL is missing")
        if Settings.is_production and not os.getenv("ADDRESS_PROVIDER_URL", "").strip().startswith("https://"):
            missing.append("ADDRESS_PROVIDER_URL must use https:// in production")
        if Settings.is_production and not os.getenv("ADDRESS_PROVIDER_TOKEN", "").strip():
            missing.append("ADDRESS_PROVIDER_TOKEN is missing")
    elif provider_mode in {"local_hd", "local", "seed"}:
        if not os.getenv("HD_WALLET_SEED_HEX", "").strip():
            missing.append("HD_WALLET_SEED_HEX is missing")
    else:
        missing.append(f"unsupported ADDRESS_PROVIDER mode: {provider_mode}")

    if _withdrawals_enabled():
        provider = os.getenv("WITHDRAWAL_PROVIDER", os.getenv("SIGNER_PROVIDER", "")).strip().lower()
        if not provider:
            missing.append("WITHDRAWAL_PROVIDER is missing while withdrawals are enabled")
        elif provider != "http" and Settings.is_production:
            missing.append("WITHDRAWAL_PROVIDER must be 'http' in production when withdrawals are enabled")
        if provider == "http":
            if not os.getenv("WITHDRAWAL_PROVIDER_URL", "").strip():
                missing.append("WITHDRAWAL_PROVIDER_URL is missing")
            if Settings.is_production and not (os.getenv("WITHDRAWAL_PROVIDER_URL", "").strip().startswith("https://") or os.getenv("WITHDRAWAL_PROVIDER_URL", "").strip().startswith("http://127.0.0.1") or os.getenv("WITHDRAWAL_PROVIDER_URL", "").strip().startswith("http://localhost")):
                missing.append("WITHDRAWAL_PROVIDER_URL must use https:// in production unless it is loopback http://127.0.0.1 or http://localhost")
            if Settings.is_production and not os.getenv("WITHDRAWAL_PROVIDER_TOKEN", "").strip():
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

    if env_flag_enabled("SOL_WATCHER_ENABLED", False):
        sol_status, sol_error = _run_preflight("sol_watcher")
        if sol_status is None:
            blocked.append(f"sol watcher startup preflight failed: {sol_error or 'unknown'}")
            checks.append(("sol_watcher", "fail", sol_error or "sol watcher preflight failed"))
        else:
            checks.append(("sol_watcher", "pass", "ok"))
    else:
        checks.append(("sol_watcher", "skip", "SOL_WATCHER_ENABLED=false"))

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
            checks.append(("deposit_provider", "pass" if dep_ready else "fail", _safe_reason(dep_reason or "ok")))
            if not dep_ready:
                blocked.append(f"deposit provider unavailable: {_safe_reason(dep_reason or 'not ready')}")

            if _withdrawals_enabled():
                signer = SignerService()
                wd_ready, wd_reason = signer.readiness()
                checks.append(("withdrawal_provider", "pass" if wd_ready else "fail", _safe_reason(wd_reason or "ok")))
                checks.append(("signer_readiness", "pass" if wd_ready else "fail", _safe_reason(wd_reason or "ok")))
                if not wd_ready:
                    blocked.append(f"withdrawal provider/signer unavailable: {_safe_reason(wd_reason or 'not ready')}")
            else:
                checks.append(("withdrawal_provider", "skip", "WITHDRAWALS_ENABLED=false"))
                checks.append(("signer_readiness", "skip", "WITHDRAWALS_ENABLED=false"))
        except Exception as exc:
            blocked.append(f"provider readiness check failed: {_safe_reason(exc)}")
            checks.append(("provider_readiness", "fail", _safe_reason(exc)))
        finally:
            conn.close()

    sqlite_path = _effective_sqlite_path() or ":memory:"
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
