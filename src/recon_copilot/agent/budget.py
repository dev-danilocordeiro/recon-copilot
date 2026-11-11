"""Token ceiling, enforced before each API call so a run can't overshoot it.

Counted tokens = input + cache writes + cache reads + output, as reported by the API.
Before a call we reserve `counted_input + max_tokens` (the most that call could use);
if that doesn't fit in what's left, the call is not made.
"""

from __future__ import annotations

from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    def __init__(self, needed: int, remaining: int) -> None:
        super().__init__(f"token ceiling reached: call needs up to {needed}, {remaining} left")
        self.needed = needed
        self.remaining = remaining


@dataclass
class TokenBudget:
    ceiling: int | None
    spent: int = 0

    @property
    def remaining(self) -> int | None:
        return None if self.ceiling is None else self.ceiling - self.spent

    def check(self, worst_case: int) -> None:
        remaining = self.remaining
        if remaining is not None and worst_case > remaining:
            raise BudgetExceeded(worst_case, remaining)

    def record(self, tokens: int) -> None:
        self.spent += tokens
