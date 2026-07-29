"""Measure the latency this host actually has.

`clock.LatencyModel` defaults to pessimistic guesses. Guesses are fine as a
default and useless as a calibration — the whole point of charging latency in a
backtest is that the number reflects the machine the bot will run on. A VPS in
Vilnius and a laptop on home wifi are different systems with different
achievable strategies.

Two quantities, and the distinction matters:

  - **Round-trip time** to the REST endpoint: how long between deciding and the
    exchange knowing. This is what delays order arrival.
  - **Feed lag**: exchange timestamp minus local arrival time on websocket
    messages. This is how stale your view of the market is.

Feed lag is contaminated by clock skew. If this host's clock is 300ms fast, the
measurement reads 300ms better than reality — in the direction that makes a
strategy look more viable than it is. So the report checks skew explicitly and
refuses to present a feed-lag number it does not trust.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

from ..data.ws import HyperliquidWS, Subscription
from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class LatencySample:
    rtt_ms: list[float] = field(default_factory=list)
    feed_lag_ms: list[float] = field(default_factory=list)

    def percentiles(self, values: list[float]) -> dict[str, float]:
        if not values:
            return {}
        s = sorted(values)
        n = len(s)
        return {
            "n": n,
            "min": round(s[0], 1),
            "p50": round(s[n // 2], 1),
            "p90": round(s[min(n - 1, int(n * 0.9))], 1),
            "p99": round(s[min(n - 1, int(n * 0.99))], 1),
            "max": round(s[-1], 1),
        }


async def measure_rtt(api_url: str, n: int = 20) -> list[float]:
    """Round trip to the info endpoint. Uses a trivial request so the number
    reflects the network, not HL's query cost."""
    import urllib.request
    import json

    out: list[float] = []
    for _ in range(n):
        body = json.dumps({"type": "meta"}).encode()
        req = urllib.request.Request(
            api_url + "/info", data=body, headers={"Content-Type": "application/json"}
        )
        t0 = time.perf_counter()
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=10).read())
        except Exception as exc:
            log.warn("rtt_probe_failed", error=str(exc))
            continue
        out.append((time.perf_counter() - t0) * 1000)
        await asyncio.sleep(0.25)
    return out


async def measure_feed_lag(ws_url: str, coin: str, seconds: int = 60) -> list[float]:
    """exchange_ms - local_ms on book updates, sign-flipped to a positive lag."""
    lags: list[float] = []
    deadline = time.time() + seconds
    async with HyperliquidWS(ws_url, [Subscription("l2Book", coin=coin)]) as ws:
        async for msg in ws:
            if time.time() > deadline:
                break
            data = msg.raw.get("data")
            if isinstance(data, dict) and "time" in data:
                lags.append(msg.local_ms - int(data["time"]))
    return lags


async def run(api_url: str, ws_url: str, coin: str = "BTC", seconds: int = 60) -> dict:
    rtt, feed = await asyncio.gather(
        measure_rtt(api_url), measure_feed_lag(ws_url, coin, seconds)
    )
    sample = LatencySample(rtt, feed)
    rtt_p = sample.percentiles(rtt)
    feed_p = sample.percentiles(feed)

    report: dict = {"rtt_ms": rtt_p, "feed_lag_ms": feed_p}

    # Clock skew shows up as a systematically negative feed lag: messages
    # appearing to arrive before the exchange stamped them.
    if feed and min(feed) < -50:
        report["clock_skew_warning"] = (
            f"minimum feed lag is {min(feed):.0f}ms — messages appear to arrive before "
            f"they were sent, so this host's clock is ahead of the exchange's. Install "
            f"and enable time sync (chrony or systemd-timesyncd) before trusting any "
            f"latency number, and before recording data you intend to backtest."
        )
        report["feed_lag_ms"] = {"untrusted": True, **feed_p}

    if rtt_p:
        # Decision latency is local compute; wire latency is half the round trip
        # for the outbound leg, which is what delays the order reaching the book.
        suggested_wire = int(rtt_p["p90"] / 2)
        report["suggested_latency_model"] = {
            "decision_ms": 25,
            "wire_ms": max(20, suggested_wire),
            "note": (
                "put these in the LatencyModel used by the backtester. Using the p90 "
                "rather than the median is deliberate: the slow cases are the ones "
                "that cost money."
            ),
        }
        if rtt_p["p90"] > 400:
            report["verdict"] = (
                f"p90 round trip is {rtt_p['p90']:.0f}ms. Any strategy whose edge decays "
                f"in under a second is not reachable from this host. Horizons of minutes "
                f"are unaffected."
            )
        else:
            report["verdict"] = (
                f"p90 round trip {rtt_p['p90']:.0f}ms — adequate for multi-minute holding "
                f"periods. Not competitive with colocated market makers, which no VPS is."
            )
    return report
