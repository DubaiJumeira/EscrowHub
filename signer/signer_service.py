from __future__ import annotations

import os


class SignerService:
    """Isolated signer process boundary. Keep secrets outside bot process/repo."""

    def __init__(self) -> None:
        self.mode = os.getenv("SIGNER_MODE", "mock")

    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str) -> str:
        if self.mode == "mock":
            return f"mock_{asset.lower()}_{destination_address[-6:]}_{amount}"
        raise NotImplementedError("Production signer integration must use secure secret manager")
