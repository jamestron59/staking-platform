"""Live Hyperliquid gateway.

Every design choice here is about one question: what happens when a call does
not return cleanly?

**Idempotency.** The `cloid` is minted before the first attempt. On a timeout we
do not retry the order — we ask the exchange what happened to that cloid via
`query_order_by_cloid`, and only then decide. Blind retries are how a bot ends
up with double the intended position at the worst possible moment.

**Action expiry.** Every signed action carries `expiresAfter`. A request stuck
in a proxy for 30 seconds cannot execute when it finally arrives. Without this,
a network stall becomes an order placed into a market that has already moved.

**Dead man's switch.** `scheduleCancel` tells HL to cancel all resting orders
at a future timestamp unless refreshed. If this process dies, loses network, or
hangs, HL cleans up on our behalf. HL permits 10 triggers per UTC day and needs
at least 5 seconds of lead time, so the refresher runs well inside that budget
and counts its own triggers.

**Agent wallets.** The configured key should be an approved agent (API) wallet.
It can trade but cannot withdraw, so a compromised host loses the open
positions, not the balance. `verify_permissions` checks this at boot.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.signing import Cloid

from ..config import Config
from ..instruments import InstrumentRegistry
from ..logging_setup import get_logger
from ..types import Fill, Order, OrderStatus, Position, Side, TimeInForce
from .gateway import AccountSnapshot, SubmitResult

log = get_logger(__name__)


class HyperliquidGateway:
    def __init__(self, cfg: Config, registry: Optional[InstrumentRegistry] = None) -> None:
        self.cfg = cfg
        secret = cfg.account.load_secret()
        self._wallet = Account.from_key(secret)
        self._info = Info(cfg.network.api_url, skip_ws=True)
        self._exchange = Exchange(
            self._wallet,
            cfg.network.api_url,
            # HL routes subaccount actions through vaultAddress.
            vault_address=cfg.vault_address,
            account_address=cfg.account.account_address or None,
        )
        self.registry = registry or InstrumentRegistry.from_meta(self._info.meta())
        self.address = cfg.trading_address
        self._dms_triggers_today = 0
        self._dms_day = ""
        self._dms_task: Optional[asyncio.Task] = None
        self._closed = False

    # ---- boot-time safety -------------------------------------------------

    async def verify_permissions(self) -> dict[str, Any]:
        """Refuses to start on an obviously unsafe configuration.

        The signer being the same address as the account means the master key is
        on this host. That is a withdrawal-capable key sitting next to a process
        that talks to the internet, and no amount of downstream risk management
        compensates for it.
        """
        signer = self._wallet.address
        report = {
            "signer": signer,
            "trading_address": self.address,
            "using_subaccount": bool(self.cfg.vault_address),
            "is_agent_wallet": signer.lower() != (self.cfg.account.account_address or "").lower(),
        }
        if self.cfg.account.require_agent_wallet and not report["is_agent_wallet"]:
            raise RuntimeError(
                "signer equals account address: this is a withdrawal-capable master key. "
                "Approve an agent (API) wallet and point HL_API_SECRET at it, or set "
                "account.require_agent_wallet: false to override deliberately."
            )
        state = await asyncio.to_thread(self._info.user_state, self.address)
        report["account_value"] = float(state["marginSummary"]["accountValue"])
        report["withdrawable"] = float(state.get("withdrawable", 0))
        log.event("gateway_permissions_verified", **report)
        return report

    # ---- order lifecycle --------------------------------------------------

    async def submit(self, order: Order) -> SubmitResult:
        inst = self.registry.get(order.coin)
        if not inst.is_valid_price(order.limit_px) or not inst.is_valid_size(order.sz):
            # The SDK raises inside float_to_wire for these; catching it here
            # keeps the failure attributable instead of surfacing as a signing error.
            return SubmitResult(
                ok=False, cloid=order.cloid,
                error=f"invalid tick/lot: px={order.limit_px} sz={order.sz} szDecimals={inst.sz_decimals}",
            )

        self._exchange.set_expires_after(
            int(time.time() * 1000) + self.cfg.execution.action_expiry_ms
        )
        try:
            resp = await asyncio.to_thread(
                self._exchange.order,
                order.coin,
                order.side is Side.BUY,
                order.sz,
                order.limit_px,
                {"limit": {"tif": order.tif.value}},
                order.reduce_only,
                Cloid.from_str(order.cloid),
            )
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            # The order may or may not have landed. This is the state that
            # requires resolution, not retry.
            log.error("order_indeterminate", cloid=order.cloid, error=str(exc))
            return SubmitResult(ok=False, cloid=order.cloid, error=str(exc), indeterminate=True)
        except Exception as exc:
            log.exception("order_submit_failed", cloid=order.cloid)
            return SubmitResult(ok=False, cloid=order.cloid, error=str(exc))
        finally:
            self._exchange.set_expires_after(None)

        return self._parse_order_response(resp, order)

    def _parse_order_response(self, resp: Any, order: Order) -> SubmitResult:
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            err = str(resp)
            order.status, order.error = OrderStatus.REJECTED, err
            return SubmitResult(ok=False, cloid=order.cloid, error=err)

        statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            return SubmitResult(ok=False, cloid=order.cloid, error="empty statuses", indeterminate=True)

        st = statuses[0]
        if "error" in st:
            order.status, order.error = OrderStatus.REJECTED, st["error"]
            return SubmitResult(ok=False, cloid=order.cloid, error=st["error"])

        if "resting" in st:
            order.oid = int(st["resting"]["oid"])
            order.status = OrderStatus.OPEN
            return SubmitResult(ok=True, cloid=order.cloid, oid=order.oid)

        if "filled" in st:
            f = st["filled"]
            px, sz, oid = float(f["avgPx"]), float(f["totalSz"]), int(f["oid"])
            order.oid = oid
            order.apply_fill(px, sz, int(time.time() * 1000))
            fill = Fill(
                coin=order.coin, exchange_ms=int(time.time() * 1000), px=px, sz=sz,
                side=order.side, fee=0.0, oid=oid, tid=0,
                crossed=order.tif is not TimeInForce.ALO, cloid=order.cloid,
            )
            return SubmitResult(ok=True, cloid=order.cloid, oid=oid, immediate_fill=fill)

        return SubmitResult(ok=False, cloid=order.cloid, error=f"unrecognised status {st}", indeterminate=True)

    async def resolve_indeterminate(self, order: Order) -> Optional[Order]:
        """Ask the exchange what actually happened to a cloid we lost track of.

        Called instead of retrying. If HL knows the cloid, the order landed and
        our local state must adopt the exchange's version; if it does not, the
        order never existed and may be re-sent with the same cloid.
        """
        for attempt in range(5):
            await asyncio.sleep(min(2 ** attempt, 8) * 0.5)
            found = await self.query_by_cloid(order.cloid)
            if found is not None:
                log.warn("indeterminate_resolved_as_live", cloid=order.cloid, status=found.status.value)
                return found
        log.warn("indeterminate_resolved_as_absent", cloid=order.cloid)
        return None

    async def query_by_cloid(self, cloid: str) -> Optional[Order]:
        try:
            resp = await asyncio.to_thread(
                self._info.query_order_by_cloid, self.address, Cloid.from_str(cloid)
            )
        except Exception as exc:
            log.warn("query_by_cloid_failed", cloid=cloid, error=str(exc))
            return None
        if not isinstance(resp, dict) or resp.get("status") != "order":
            return None
        o = resp["order"]["order"]
        status_raw = resp["order"].get("status", "")
        return Order(
            cloid=cloid,
            coin=o["coin"],
            side=Side.BUY if o["side"] == "B" else Side.SELL,
            sz=float(o["origSz"]),
            limit_px=float(o["limitPx"]),
            tif=TimeInForce.GTC,
            oid=int(o["oid"]),
            filled_sz=float(o["origSz"]) - float(o["sz"]),
            status={
                "open": OrderStatus.OPEN,
                "filled": OrderStatus.FILLED,
                "canceled": OrderStatus.CANCELLED,
                "rejected": OrderStatus.REJECTED,
            }.get(status_raw, OrderStatus.UNKNOWN),
            last_update_ms=int(resp["order"].get("statusTimestamp", 0)),
        )

    async def cancel(self, cloid: str, coin: str) -> bool:
        try:
            resp = await asyncio.to_thread(
                self._exchange.cancel_by_cloid, coin, Cloid.from_str(cloid)
            )
            return isinstance(resp, dict) and resp.get("status") == "ok"
        except Exception as exc:
            log.warn("cancel_failed", cloid=cloid, error=str(exc))
            return False

    async def cancel_all(self, coin: Optional[str] = None) -> int:
        orders = await asyncio.to_thread(self._info.open_orders, self.address)
        targets = [o for o in orders if coin is None or o["coin"] == coin]
        n = 0
        for o in targets:
            try:
                resp = await asyncio.to_thread(self._exchange.cancel, o["coin"], int(o["oid"]))
                n += int(isinstance(resp, dict) and resp.get("status") == "ok")
            except Exception as exc:
                log.warn("cancel_all_item_failed", oid=o.get("oid"), error=str(exc))
        log.event("cancel_all", requested=len(targets), cancelled=n, coin=coin)
        return n

    # ---- account ----------------------------------------------------------

    async def account(self) -> AccountSnapshot:
        state, open_orders = await asyncio.gather(
            asyncio.to_thread(self._info.user_state, self.address),
            asyncio.to_thread(self._info.open_orders, self.address),
        )
        positions: dict[str, Position] = {}
        for ap in state.get("assetPositions", []):
            p = ap["position"]
            szi = float(p["szi"])
            if abs(szi) < 1e-12:
                continue
            liq = p.get("liquidationPx")
            positions[p["coin"]] = Position(
                coin=p["coin"],
                size=szi,
                entry_px=float(p.get("entryPx") or 0.0),
                leverage=float(p.get("leverage", {}).get("value", 1)),
                # The exchange's own number: authoritative, never overridden by
                # our model in `risk.liquidation`.
                liquidation_px=float(liq) if liq else None,
                unrealized_pnl=float(p.get("unrealizedPnl", 0.0)),
                margin_used=float(p.get("marginUsed", 0.0)),
            )
        ms = state["marginSummary"]
        return AccountSnapshot(
            equity_usd=float(ms["accountValue"]),
            positions=positions,
            open_orders=[
                Order(
                    cloid=o.get("cloid") or "",
                    coin=o["coin"],
                    side=Side.BUY if o["side"] == "B" else Side.SELL,
                    sz=float(o["origSz"]),
                    limit_px=float(o["limitPx"]),
                    tif=TimeInForce.GTC,
                    oid=int(o["oid"]),
                    filled_sz=float(o["origSz"]) - float(o["sz"]),
                    status=OrderStatus.OPEN,
                )
                for o in open_orders
            ],
            margin_used=float(ms.get("totalMarginUsed", 0.0)),
            withdrawable=float(state.get("withdrawable", 0.0)),
            at_ms=int(time.time() * 1000),
        )

    async def recent_fills(self, since_ms: int) -> list[Fill]:
        raw = await asyncio.to_thread(self._info.user_fills, self.address)
        out = []
        for f in raw:
            if int(f["time"]) < since_ms:
                continue
            out.append(Fill(
                coin=f["coin"], exchange_ms=int(f["time"]), px=float(f["px"]), sz=float(f["sz"]),
                side=Side.BUY if f["side"] == "B" else Side.SELL, fee=float(f.get("fee", 0.0)),
                oid=int(f.get("oid", 0)), tid=int(f.get("tid", 0)), crossed=bool(f.get("crossed", True)),
                cloid=f.get("cloid"), closed_pnl=float(f.get("closedPnl", 0.0)),
                start_position=float(f.get("startPosition", 0.0)),
            ))
        return out

    # ---- dead man's switch ------------------------------------------------

    async def refresh_dead_man_switch(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self._dms_day:
            self._dms_day, self._dms_triggers_today = day, 0
        if self._dms_triggers_today >= 9:
            # HL caps this at 10 per UTC day. Burning the last one leaves us
            # with no automatic cleanup for the rest of the day.
            log.warn("dead_man_switch_budget_exhausted", triggers=self._dms_triggers_today)
            return
        deadline = int(time.time() * 1000) + self.cfg.execution.dead_man_switch_s * 1000
        try:
            await asyncio.to_thread(self._exchange.schedule_cancel, deadline)
            self._dms_triggers_today += 1
        except Exception as exc:
            log.warn("dead_man_switch_refresh_failed", error=str(exc))

    async def start_dead_man_switch(self) -> None:
        async def loop() -> None:
            while not self._closed:
                await self.refresh_dead_man_switch()
                await asyncio.sleep(self.cfg.execution.dead_man_refresh_s)

        self._dms_task = asyncio.create_task(loop())
        log.event(
            "dead_man_switch_started",
            window_s=self.cfg.execution.dead_man_switch_s,
            refresh_s=self.cfg.execution.dead_man_refresh_s,
        )

    async def close(self) -> None:
        self._closed = True
        if self._dms_task:
            self._dms_task.cancel()
        try:
            # Clear the scheduled cancel so a clean shutdown does not leave a
            # timer that fires against a later, manually placed order.
            await asyncio.to_thread(self._exchange.schedule_cancel, None)
        except Exception:
            pass
