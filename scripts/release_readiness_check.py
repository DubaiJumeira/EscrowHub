from __future__ import annotations

import argparse
import json
import sys

from readiness_service import READINESS_BLOCKED, assess_release_readiness


def _render_human(report) -> str:
    lines = [f"Overall status: {report.status}"]
    lines.append("Checks:")
    for name, state, detail in report.checks:
        lines.append(f"- {name}: {state.upper()} ({detail})")
    if report.blocked_reasons:
        lines.append("Blocked reasons:")
        for reason in report.blocked_reasons:
            lines.append(f"- {reason}")
    if report.degraded_reasons:
        lines.append("Degraded reasons:")
        for reason in report.degraded_reasons:
            lines.append(f"- {reason}")
    if report.warnings:
        lines.append("Warnings:")
        for warning in report.warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="EscrowHub release readiness check")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    parser.add_argument("--allow-degraded", action="store_true", help="Allow DEGRADED to pass with zero exit code")
    args = parser.parse_args()

    report = assess_release_readiness(allow_degraded=args.allow_degraded)
    payload = {
        "status": report.status,
        "blocked_reasons": list(report.blocked_reasons),
        "degraded_reasons": list(report.degraded_reasons),
        "warnings": list(report.warnings),
        "checks": [{"name": n, "state": s, "detail": d} for n, s, d in report.checks],
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(_render_human(report))

    # WARNING: blocked readiness exits non-zero to fail closed before launch.
    return 2 if report.status == READINESS_BLOCKED else 0


if __name__ == "__main__":
    raise SystemExit(main())
