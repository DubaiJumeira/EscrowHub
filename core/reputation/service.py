from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Reputation:
    completed_trades: int = 0
    disputes: int = 0

    @property
    def score(self) -> float:
        penalty = self.disputes * 2
        return max(0.0, 100.0 - penalty)


class ReputationService:
    def __init__(self) -> None:
        self._stats: dict[int, Reputation] = {}

    def _get(self, user_id: int) -> Reputation:
        if user_id not in self._stats:
            self._stats[user_id] = Reputation()
        return self._stats[user_id]

    def record_completed_trade(self, user_id: int) -> None:
        self._get(user_id).completed_trades += 1

    def record_dispute(self, user_id: int) -> None:
        self._get(user_id).disputes += 1

    def get_profile(self, user_id: int) -> Reputation:
        return self._get(user_id)
