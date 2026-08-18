# DONmarket

A read, analysis and (eventually) execution engine for [Polymarket](https://polymarket.com)
prediction markets.

*[Version française](README.fr.md) — the French README is the primary engineering log.*

---

## What this repository actually is

Most trading repositories publish a strategy and a backtest that works. This one
publishes **eight measurements, four of which killed the strategy that motivated
them** — and it keeps the disproven claims on the page, struck through, rather
than quietly deleting them.

The headline result, measured on **1,937 markets** with both order books readable:

| Cost to buy a complete set (YES + NO) | Markets | Share |
|---|---|---|
| < $1.000 — **arbitrage exists** | **0** | **0 %** |
| $1.000 – $1.0015 — locked at the tick | 724 | 37.4 % |
| $1.0015 – $1.010 | 434 | 22.4 % |
| > $1.010 — loose book | 779 | 40.2 % |

On the sell side, the best observed bid sum is **0.999** — exactly one tick
*below* 1.

**Conclusion, stated plainly:** the "YES + NO < $1" arbitrage is not rare on
Polymarket, it is **structurally absent**. Market makers hold the ask sum one
tick above 1 and the bid sum one tick below. The tick is 0.001; the spread never
crosses. Every "$100 → $1,787 overnight" claim built on this arbitrage is false,
and the table above is the numerical proof.

That is a useful result, not a failure: it closes a line of investigation for
good instead of letting it keep costing time and capital.

## Builders Radar — what the builder leaderboard hides

**→ [Live page](https://midas93230-cell.github.io/donmarket/)** · regenerate with
`python -m donmarket builder --period WEEK`

Polymarket ranks its builders by **volume**. Volume is not revenue, and the gap
between the two is the entire economics of the program. Measured 2026-08-15 on
the top 25 builders, from three public endpoints that need no API key:

| Finding | Evidence |
|---|---|
| **13 of the 25 largest builders charge 0 bps** | The volume leader routes $29M a week and collects nothing from it. Ranking by volume tells you almost nothing about who earns. |
| **The published fee cap is not enforced** | Docs state a 100 bps taker maximum. **MetaMask and RedotPay both charge 400.00 bps** — dispersion 0.000 across 261 fills. |
| **The fee base is USDC notional, not shares** | The documentation says "notional" without settling it, and on a binary market a share pays $1 at resolution — the two readings differ by a factor of 1/price (×25 at $0.04). Implied-rate dispersion: **0.000–0.49** against notional, **0.80–2.34** against shares, no overlap. |
| **Platform fee follows variance, not notional** | `rate × shares × p × (1−p)`, not `min(p, 1−p)`. Dispersion 1.03 against 1.91 across 774 taker fills. The rate is per-market: 0.0280 to 0.0720. |

None of these rates are published anywhere. Each is inferred from a builder's own
attributed executions, using the **maximum** implied rate rather than the median —
payouts truncate downward, so a fill's implied rate can only fall below the
configured one. Observed maxima are all round numbers (5, 10, 25, 50, 100, 400);
the medians are not.

**What is deliberately not claimed:** the unit of the leaderboard's `volume`
field is unverified — confirming it would require draining a builder's full
history, and even the smallest of the top 50 exceeds 12,000 executions. Every
revenue figure is therefore an *estimate*, and `RevenueEstimate.is_measured`
returns `False` to keep any caller from forgetting it.

## Status

**Built and tested against the live API:**

- Full open-market universe read (2,100 markets — the API ceiling).
- Real order books fetched in parallel batches (4,200 books).
- Complete-set arbitrage detection engine, two threshold regimes.
- **Liquidity-rewards strategy**, measured and wired into the CLI: full funnel,
  quantified inventory risk, portfolio constraint.
- **Real-time WebSocket feed** on selected candidate tokens, with continuous
  yield recomputation (protocol verified by reconciliation against REST).
- **Time-averaged** competing liquidity — the snapshot is only one draw
  (fourth measurement).
- **Local dashboard** (`serve`), loopback only, zero external dependencies.
- **N-model ensemble vote** (`consensus`), built and measured — verdict in the
  fifth measurement.
- **Reward scoring using Polymarket's published formula**, including the effect
  of our own order on the midpoint (sixth measurement).
- **Historical market-making replay** (`backtest`), which disproved the risk
  upper bound and replaced it (seventh measurement).
- **Execution engine** (`trade`), disarmed by default: without `--arm` the full
  path runs, caps included, and stops before signing. **No real order has ever
  been sent** — see "What is not done".
- **Paper account** (`paper`): fictional capital, orders matched against real
  market executions, rewards scored on orders as they actually rest
  (eighth measurement).
- SQLite persistence (market, scan and opportunity history).
- **489 tests**, including five regressions that were paid for the hard way
  (see "Pitfalls").

**Measured performance:** 2,100 markets + 4,200 books analysed in **13.5 s**;
a full rewards scan (2,099 markets, 948 books, 60 minute-resolution histories)
in **48–71 s**.

## The eight measurements

Each section in the [French README](README.fr.md) carries the full reasoning and
raw tables. Summarised:

| # | Date | Question | Verdict |
|---|---|---|---|
| 1 | 2026-07-28 | Does taker-side complete-set arbitrage exist? | **No — 0 / 1,937.** Structurally absent. |
| 2 | 2026-07-28 | Does maker-side work, on *realised fills*? | **No.** +0.57 % median, negative where volume is; 27 % adverse selection. |
| 3 | 2026-07-28 | Are liquidity rewards free money? | **No.** Median **−1.60 %/day** net of inventory drift. Only 16/60 cover their own risk. |
| 4 | 2026-07-29 | Is a scan's number still true when printed? | **No.** Overstated 2–5×. Competition swings 3× in seconds. |
| 5 | 2026-07-29 | Does the "28 of 31 models must agree" method work? | **No.** At 24/31+ it takes **0.0 %** of decisions. Only 8.3 of 31 votes are independent. |
| 6 | 2026-07-31 | Does our own order move the midpoint it is scored against? | **Yes.** Top of ranking fell from +238 %/day to **+21.90 %/day** once corrected. |
| 7 | 2026-08-01 | Is drift an upper bound on inventory cost? | **No.** Broke on 6 of 17 quoted markets, by up to **31.5 points/day**. |
| 8 | 2026-08-06 | Was the paper account honest? | **No.** A session with **zero orders** credited itself **$9.09/day**. Root cause: `PaperSession` had no tests. |

Two of these deserve their own note.

### The ensemble vote is caught in a pincer (fifth measurement)

A method circulating on social media: run N prediction models in parallel, only
trade when 28 of 31 agree. The underlying technique is real — ECMWF runs 51
perturbed members — so it was built as specified (`donmarket/consensus/`) and
measured on 40 Polymarket markets.

| Threshold | Decisions taken |
|---|---|
| 31/31 | **0.0 %** |
| 28/31 | **0.0 %** |
| 24/31 | **0.0 %** |
| 20/31 | 1.9 % |
| 12/31 | 11.6 % |
| 8/31 | 29.3 % |
| 6/31 | **81.9 %** |

Mean inter-member correlation is **+0.090**; only **8.3 of 31** votes are
genuinely independent. If members are diverse enough to be worth anything they
never agree 28-of-31 and the system never trades. If they do agree 28-of-31 they
are copies, and the vote is measuring its own redundancy while looking rigorous.
Between 8/31 (33 %) and 6/31 (82 %) there is no threshold that is both selective
and active.

### Our own order moves the midpoint it is scored against (sixth measurement)

Posting at `m − v/2` makes our order the **best bid**. The midpoint moves toward
it, and the final distance is not `v/2` but `(A−B)/2 + v/2`. **We only score if
the book spread is at most three times the band.**

Worse, it was systematic: `competing_q = 0` and "our score is zero" are the
**same phenomenon** — a gaping book. Unsellable markets were therefore ranked
**first**, precisely because they looked deserted.

| | Before | After |
|---|---|---|
| Top of ranking | +238 %/day | **+21.90 %/day** |
| Sustainable portfolio at $100 | +$70.81/day | **+$12.70/day** |

## Verified API pitfalls

Eleven traps confirmed against the live API, documented so they need not be
rediscovered. A selection:

1. **Order books are not sorted usefully.** `bids` arrives in ascending price
   order and `asks` descending — the best price is **last**. Trusting `bids[0]`
   gives you the worst price in the book.
2. **Spread must be measured branch by branch.** Comparing the No ask to the Yes
   bid yields spreads of 0.96 on books that are actually tight to 0.001.
3. **Gamma caps at 100 markets per page** regardless of the limit requested, and
   returns **422 beyond offset 2100**. A "short page = done" stop condition
   therefore halts pagination on page one.
4. **`rewardsMaxSpread` is in percent, not dollars.** `3.0` means 3 *cents*.
   Without the division by 100, the entire book depth falls inside the
   qualifying band and competition is massively overstated.
5. **A resolved market can stay `closed=false`** — its reward pool intact and its
   competing liquidity nil, producing phantom yields (118 %/day observed).
   Filter on `endDate`, not on `closed`.
6. **In a `price_change`, `size` is the NEW level size**, not a delta; zero
   removes the level. Getting this backwards raises no error — the book drifts
   slowly and every yield computed on it stays plausible.

The full list of eleven is in the [French README](README.fr.md).

## Usage

```bash
python -m donmarket scan --mode serieux --bankroll 14.47
python -m donmarket scan --mode normal --max-markets 300
python -m donmarket rewards --bankroll 100
python -m donmarket serve --bankroll 100        # http://127.0.0.1:8787
python -m donmarket consensus --members 31 --threshold 28
python -m donmarket backtest
python -m donmarket paper --bankroll 100 --minutes 30
python -m donmarket stats
python -m pytest tests -q

# The only path that can commit money. Without --arm, nothing is sent.
python -m donmarket trade --max-total 20 --max-per-market 10
```

No API key is required **except for `trade --arm`**: everything else runs on
public read-only endpoints, including `paper`, which sends no orders.

Copy `.env.example` to `.env` and fill it in only if you intend to arm execution.
`.env` is gitignored.

## Routing your volume through DONmarket

Polymarket lets an application attribute the volume it routes and charge a fee on
it. That is how this repository is funded, and the terms are stated here rather
than buried, because **the fee is paid by you, the trader, on your own fills** —
not by Polymarket, and not by us.

| | Rate |
|---|---|
| Taker fills | **10 bps** (0.10%) |
| Maker fills | **5 bps** (0.05%) |

For scale: two of the largest builders on the platform charge **400 bps** and
publish no rate at all. Ours is on this page because a number you have to
reverse-engineer from your own fills is not a disclosed number.

**What you get for it.** Everything in this repository, which you can also use
entirely for free — attribution is opt-in and off by default. What routing buys
is not a feature gate; it is whether the work continues. If the eight
measurements above saved you from one bad position, that is the trade.

**Turning it on** — two lines in your `.env`:

```
POLYMARKET_BUILDER_REMOTE_URL=https://donmarket-signer.midas93230.workers.dev
POLYMARKET_BUILDER_REMOTE_TOKEN=<issued individually — ask>
```

`python -m donmarket builder` then reports `attribution_mode: remote`. Your
private key never leaves your machine, and you never hold our builder secret.

**What the signer sees, stated plainly.** Polymarket's attribution headers sign
the *body* of the order, so the signing service receives each order before it
reaches the book. That is inherent to the protocol, not a design choice, and it
is the first objection a serious market maker should raise. In answer: the
service is one readable file ([`signer/worker.js`](signer/worker.js)), it logs
neither order bodies nor headers nor tokens, and it stores nothing. Signature
parity against the Python SDK is verified before every deploy, and was verified
again in production on 2026-08-18 — four cases, four identical signatures.

**Leaving both lines empty is entirely legitimate.** Your orders go out
unattributed, no fee is charged, and every command behaves identically.

## What is not done

- **The armed path has never run.** No order has ever been sent. The tests verify
  what the engine **refuses** to do, not what it succeeds at: an EIP-712
  signature that passes tests and is wrong in production costs real dollars, and
  only a first tiny live order will validate it. That is the account owner's
  decision, not the code's.
- **The wide-book entry premium is not modelled.** Once the book spread exceeds
  three times the band, scoring would require posting at `A − v` — sometimes 20
  cents above the best bid. That immediate overpayment enters no risk
  calculation. Until it does, the correct answer is not to play those markets,
  which is what the code does, rejecting them as "book too wide".
- **Jump risk is not modelled.** A 24-hour history says nothing about a market
  resolving on a discrete publication — the price jumps rather than drifts.
- **Shadow mode is not wired.** `execute/shadow.py` is written and tested but has
  no caller: the pool share actually obtained against real orders of ours remains
  the one term nothing has measured.

## Legal note

France's ANJ ordered ISP-level blocking of Polymarket on 2026-07-16. This
repository contains no means of circumventing that block and will not add one.

## License

MIT — see [LICENSE](LICENSE).
