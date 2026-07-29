"""Gateway interface.

Paper and live implement the same protocol so the supervisor, risk engine and
strategy code cannot tell them apart. That is not tidiness — it is the only way
a paper run is evidence about the live run. A separate "paper mode" branch
inside the trading loop tests the paper branch and nothing else.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from ..types import Fill, Order, Position


def new_cloid() -> str:
    """128-bit client order id, HL's required format (0x + 32 hex chars).

    Generated once, before the first send attempt, and reused across retries.
    That is what makes a retry after a timeout safe: if the first attempt
    actually landed, the exchange rejects the duplicate rather than opening a
    second position.
    """
    return "0x" + secrets.token_hex(16)


@dataclass
class SubmitResult:
    ok: bool
    cloid: str
    oid: Optional[int] = None
    error: str = ""
    # True when the outcome is genuinely unknown (timeout, dropped response).
    # The caller must NOT retry blindly and must NOT assume failure: it has to
    # resolve the state via `query_by_cloid`. Treating unknown as failure is how
    # you end up with double positions.
    indeterminate: bool = False
    immediate_fill: Optional[Fill] = None


@dataclass
class AccountSnapshot:
    equity_usd: float
    positions: dict[str, Position] = field(default_factory=dict)
    open_orders: list[Order] = field(default_factory=list)
    margin_used: float = 0.0
    withdrawable: float = 0.0
    at_ms: int = 0


@runtime_checkable
class Gateway(Protocol):
    async def submit(self, order: Order) -> SubmitResult: ...
    async def cancel(self, cloid: str, coin: str) -> bool: ...
    async def cancel_all(self, coin: Optional[str] = None) -> int: ...
    async def query_by_cloid(self, cloid: str) -> Optional[Order]: ...
    async def account(self) -> AccountSnapshot: ...
    async def refresh_dead_man_switch(self) -> None: ...
    async def close(self) -> None: ...
