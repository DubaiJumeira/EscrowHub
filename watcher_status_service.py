from __future__ import annotations

from datetime import datetime


def upsert_watcher_status(conn, watcher_name: str, success: bool, error: str | None = None) -> None:
    now = datetime.utcnow().isoformat()
    row = conn.execute("SELECT * FROM watcher_status WHERE watcher_name=?", (watcher_name,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO watcher_status(watcher_name,last_run_at,last_success_at,last_error,consecutive_failures,updated_at) VALUES(?,?,?,?,?,?)",
            (watcher_name, now, now if success else None, None if success else (error or ""), 0 if success else 1, now),
        )
        return

    failures = 0 if success else int(row["consecutive_failures"]) + 1
    conn.execute(
        "UPDATE watcher_status SET last_run_at=?, last_success_at=?, last_error=?, consecutive_failures=?, updated_at=? WHERE watcher_name=?",
        (
            now,
            now if success else row["last_success_at"],
            None if success else (error or ""),
            failures,
            now,
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
        }
    return out
