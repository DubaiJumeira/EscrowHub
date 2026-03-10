from __future__ import annotations

from datetime import datetime

from error_sanitizer import sanitize_runtime_error


VALID_HEALTH = {"ok", "degraded", "fatal_startup_blocked", "transient_failure"}


def map_operator_health_state(
    *,
    ready: bool,
    blocked: bool = False,
    disabled: bool = False,
    degraded: bool = False,
) -> str:
    if blocked:
        return "blocked"
    if disabled:
        return "disabled"
    if ready and not degraded:
        return "ready"
    return "degraded"


def upsert_watcher_status(
    conn,
    watcher_name: str,
    success: bool,
    error: str | None = None,
    health: str | None = None,
) -> None:
    now = datetime.utcnow().isoformat()
    row = conn.execute("SELECT * FROM watcher_status WHERE watcher_name=?", (watcher_name,)).fetchone()
    if success:
        status_health = "ok"
    else:
        status_health = health if health in VALID_HEALTH else "transient_failure"
    safe_error = None if success else sanitize_runtime_error(error)
    if not row:
        conn.execute(
            "INSERT INTO watcher_status(watcher_name,last_run_at,last_success_at,last_error,consecutive_failures,updated_at,health_state) VALUES(?,?,?,?,?,?,?)",
            (watcher_name, now, now if success else None, safe_error, 0 if success else 1, now, status_health),
        )
        return

    failures = 0 if success else int(row["consecutive_failures"]) + 1
    conn.execute(
        "UPDATE watcher_status SET last_run_at=?, last_success_at=?, last_error=?, consecutive_failures=?, updated_at=?, health_state=? WHERE watcher_name=?",
        (
            now,
            now if success else row["last_success_at"],
            safe_error,
            failures,
            now,
            status_health,
            watcher_name,
        ),
    )


def read_watcher_status(conn, watcher_names: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in watcher_names:
        row = conn.execute("SELECT * FROM watcher_status WHERE watcher_name=?", (name,)).fetchone()
        out[name] = dict(row) if row else {
            "watcher_name": name,
            "last_run_at": None,
            "last_success_at": None,
            "last_error": None,
            "consecutive_failures": 0,
            "updated_at": None,
            "cursor": None,
            "health_state": "ok",
        }
    return out


def read_watcher_cursor(conn, watcher_name: str) -> int | None:
    row = conn.execute("SELECT cursor FROM watcher_status WHERE watcher_name=?", (watcher_name,)).fetchone()
    if not row:
        return None
    value = row["cursor"]
    return int(value) if value is not None else None


def write_watcher_cursor(conn, watcher_name: str, cursor: int) -> None:
    row = conn.execute("SELECT watcher_name FROM watcher_status WHERE watcher_name=?", (watcher_name,)).fetchone()
    now = datetime.utcnow().isoformat()
    if row:
        conn.execute("UPDATE watcher_status SET cursor=?, updated_at=? WHERE watcher_name=?", (int(cursor), now, watcher_name))
        return
    conn.execute(
        "INSERT INTO watcher_status(watcher_name,last_run_at,last_success_at,last_error,consecutive_failures,updated_at,cursor,health_state) VALUES(?,?,?,?,?,?,?,?)",
        (watcher_name, None, None, None, 0, now, int(cursor), "ok"),
    )
