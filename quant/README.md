# hlq — Hyperliquid systematic trading stack

A trading system built so that the parts which lose money are the parts under
the most scrutiny. It is not a strategy. It is the machinery a strategy needs
in order for its results to mean anything.

**Nothing here predicts the market.** The one component that could — the model
layer — cannot be trained until you have recorded weeks of data, and the
registry refuses to give an untrained or unvalidated model any capital. That is
deliberate.

```
                        Hyperliquid WS / REST
                                 │
                    ┌────────────┴────────────┐
                    │      Recorder (raw)     │   Phase 0 — capture everything
                    └────────────┬────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │   Replay (arrival order)│
                    └────────────┬────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │  FeaturePipeline (PIT)  │◄── same object used live
                    └────────────┬────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │        Strategy         │   proposes only
                    └────────────┬────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │  RiskEngine + CostModel │   the only component that says no
                    └────────────┬────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │  Router → Gateway       │   paper and live share this path
                    └────────────┬────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
        Reconciler         KillSwitches        Journals
     (exchange = truth)   (sticky, manual     (trades + rejected
                           reset only)         signals)
```

---

## How this differs from the architecture it came from

The original design put five AI models and a meta-model at the centre, with
risk and execution as downstream boxes. This inverts that, for reasons that are
each encoded in a specific module:

| Original | Here | Why |
|---|---|---|
| 5 sub-AIs + Meta AI | one model, meta-labelling optional | Sub-models share one feature source, so their errors correlate. The meta-model reads five numbers that look independent and are not, producing confident agreement at exactly the wrong moments. |
| "Confidence 0-100, trade above 80" | expected value vs. cost | `model/calibration.py`. An 88% signal on a 5bps target with a 40bps stop has negative expectancy. Confidence cannot see that. |
| 100+ indicators | 16 near-orthogonal features | `features/library.py`. EMA20/50/200 + MACD are one piece of information stated four times. |
| Spoofing / iceberg detection | book slope, absorption, depth decay | HL's L2 feed is aggregated by price. Order lifecycle tracking is not possible, so those detectors would be noise with a confident name. |
| News AI as alpha | economic calendar as a risk veto | Social sentiment is adversarial — pump groups post *in order to* trigger bots. |
| Fixed 30/50/20 entry tranches | impact-driven slicing | `execution/router.py`. Scaling in on a schedule worsens the average entry when the signal is right. What justifies slicing is depth, not habit. |
| Retrain every 1,000 trades | time-based walk-forward + shadow period | `model/registry.py`. 1,000 trades on a 1m horizon is one regime. And retraining on your own fills is a censored sample. |
| Costs unmentioned | `costs.py`, gating every decision | A taker round trip is ~9bps in fees. Most 5-minute directional edges are smaller than that. |

---

## The phases, in order

Each phase must produce a green result before the next one starts. The CLI is
the runbook.

### Phase 0 — Record (weeks, not hours)

```bash
cp config/config.example.yaml config/config.yaml   # edit coins
hlq record
```

Market data is not reproducible. A day not recorded is gone forever. Start this
before you have a strategy, before you have an opinion, before anything else.

```bash
hlq verify        # coverage gaps, reconnect counts, per-channel volumes
```

Non-zero exit means holes in the data. A model trained across a six-hour gap
will still train — it will just be wrong in a way nothing downstream detects.

### Phase 1 — Backtest

```bash
hlq backtest --n-trials 40
```

`--n-trials` is how many strategy variants you have tried **in total**, including
the ones you discarded. It feeds the deflated Sharpe ratio. Be honest with it:
its entire purpose is to tell you that the Sharpe of 2.4 you found on attempt 40
is what pure noise produces on attempt 40.

The command exits non-zero unless the result clears every gate in
`BacktestResult.verdict()`. **Most ideas will not clear it.** That is the
correct outcome, not a bug to work around.

### Phase 2 — Paper, then live plumbing

```bash
hlq paper       # live data, simulated fills, identical code path
```

Run this for days, not minutes. What you are testing is not the strategy — it
is reconnects, partial fills, feature warmup after a gap, reject handling, and
whether the reconciler stays quiet.

Then, and only then:

```bash
hlq live        # requires typing 'trade' at the prompt
```

Start with the `FlowReversionBaseline` strategy at $20-25 per trade. It is
deliberately weak. Its job is to prove the plumbing works against the real
exchange, where the failures are ones no simulator produces. Expect roughly
break-even minus costs; if it is dramatically worse, the problem is the pipe,
and you have found that out cheaply.

### Phase 3/4 — Features and model

```bash
hlq dataset --sample-interval 60
```

Reports sample count, label balance, and how much purging removes. It will tell
you to keep recording if there is not enough data — believe it.

A model only reaches live capital via `model/registry.py`, which requires:
deflated Sharpe ≥ 0.95, positive Brier skill, calibration error ≤ 0.10, a
72-hour shadow period, ≥ 200 shadow signals, and beating the incumbent by 15%.
It then starts at 25% size. There is no path that skips this.

---

## Going live on mainnet with a subaccount

You are starting here rather than on testnet. That is a defensible choice —
testnet liquidity is fictional, so testnet fills teach you little — provided
the blast radius is genuinely bounded. Bound it like this:

**1. Create a subaccount and fund it with what you can lose.**

The subaccount is the hard limit on this bot's exposure. Not a config value —
an account boundary. Set `account.account_address` to the *subaccount* address.

**2. Use an agent (API) wallet, never your master key.**

An approved agent wallet can trade but cannot withdraw. A compromised host then
costs you open positions, not the balance. `HyperliquidGateway.verify_permissions()`
refuses to start if the signing key equals the account address.

```bash
export HL_API_SECRET=0x...      # the agent wallet key, never the master key
```

**3. Set `subaccount_address` in config.**

HL routes subaccount actions through the `vaultAddress` field, which the gateway
passes to the SDK's `Exchange`. Confirm your subaccount address with
`info.query_sub_accounts(master_address)` before the first run.

**4. Set your real fee tier.**

```python
Info(api_url).user_fees(address)
```

Put the actual numbers in `costs:`. Understating fees is the single most common
way a backtest turns a losing strategy into a winner.

**5. Start with these caps.**

```yaml
risk:
  max_position_notional_usd: 100.0
  max_gross_notional_usd: 200.0
  max_concurrent_positions: 1
  max_daily_loss_pct: 2.0
```

Raise them only after a week where the reconciler logged nothing and
`hlq status` shows realised edge tracking expected edge.

### What is protecting you while it runs

- **Dead man's switch** — `scheduleCancel` refreshed every 20s with a 60s
  horizon. If this process dies, hangs, or loses network, HL cancels all resting
  orders on its own. Budget is 10 triggers per UTC day; the gateway counts them.
- **Action expiry** — every signed action carries `expiresAfter`. A request
  stuck in a proxy cannot execute when it finally arrives.
- **Idempotent orders** — `cloid` is minted before the first send. On a timeout
  the router *never* retries; it queries `query_order_by_cloid` and resolves.
  This is what prevents double positions.
- **Reconciliation every 15s** — exchange state is truth. An order on the
  account that is not in our journal halts trading.
- **Sticky kill switches** — staleness, divergence, reject rate, daily loss,
  drawdown, losing streak. They do not reset themselves; `reset()` means a human
  looked.

Note that staleness deliberately does **not** force a flatten. Sending market
orders while blind is worse than holding, and your stops rest on the exchange,
not in this process.

---

## Layout

```
hlq/
  instruments.py      tick/lot arithmetic — verified against the SDK
  costs.py            fees, impact, funding, calibrated residual slippage
  config.py           typed config; unknown keys are a boot error
  data/               ws (reconnect + staleness), recorder, replay, book
  features/           online state machines; lookahead is structurally impossible
  risk/               sizing, portfolio correlation, liquidation, kill switches
  execution/          gateway protocol, HL gateway, paper gateway, router, reconciler
  sim/                queue-aware matching, backtest engine, deflated Sharpe
  model/              triple-barrier labels, purged CV, calibration, registry
  ops/                crash-safe state, shadow journal, supervisor
```

Run the tests:

```bash
pip install -r requirements.txt
python -m pytest tests/ -q          # 100 tests
```

The one to read first is `tests/test_features_pit.py`. It asserts that
truncating the future cannot change the past — if a feature ever peeks forward,
that test fails and every other number in the system becomes untrustworthy.

---

## Honest limitations

- **No trained model ships with this.** The pipeline is complete; the data is
  not. That is a fact about elapsed time, not about the code.
- **The baseline strategy has no meaningful edge.** It exists to test plumbing.
- **The matching engine assumes the market does not react to us.** True at
  hundreds of dollars against books with tens of thousands. Not true at size,
  which is what `max_participation_of_depth` keeps you away from.
- **The queue model ignores cancellations ahead of you**, so real fills are
  slightly better than simulated. Biased against the strategy, on purpose.
- **HL API specifics were verified against the official Python SDK source**, not
  against a live endpoint. Confirm rate limits, fee tiers and the subaccount
  `vaultAddress` routing against current docs before the first live run.
- **This architecture cannot create edge.** It can only stop you from mistaking
  noise, or your own costs, for edge. For a non-colocated participant, funding
  and basis arbitrage, cross-venue dislocations and liquidation-cascade
  participation are more likely to contain real edge than 1-minute directional
  prediction, which is the most crowded and most cost-sensitive game available.
