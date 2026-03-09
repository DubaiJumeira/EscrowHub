from __future__ import annotations


def run_once() -> int:
    # WARNING: Sweep flow is disabled because there is no complete real tx build/sign/broadcast implementation.
    # Secure alternative: implement an audited chain-specific sweep service that returns real txids before enabling this job.
    raise RuntimeError("sweep job is disabled until a production-ready signer+broadcaster path is implemented")
