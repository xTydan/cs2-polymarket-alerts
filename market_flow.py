"""Market-scoped BUY flow signals for CS2 Polymarket.

These statistics only describe the subset of buys observed by the radar.
They do NOT imply net positions, full market imbalance or coordinated trading.
"""
from __future__ import annotations

from collections import defaultdict
from math import log10

WINDOW_SECONDS = 15 * 60
BASELINE_SECONDS = 6 * 3600


def assess_market_flow(
    rows: list[tuple], outcomes: list[str], now: int,
    *, min_buy_usd: float = 2500.0,
    minimum_wallets: int = 3,
    min_share: float = 0.80,
    burst_multiplier: float = 4.0,
    baseline_min_usd: float = 500.0,
) -> list[dict]:
    """Return meaningful per-outcome BUY flows, ranked by attention score.

    rows: (timestamp, outcome, wallet, usd, price) from a single condition_id.
    Restrict all trades to a BINARY market and use unique DB transaction IDs:
    the DB's trades.id primary key has already deduplicated them.
    """
    names = {str(name).casefold().strip(): str(name) for name in outcomes if str(name).strip()}
    if len(names) != 2:
        return []
    recent: list[tuple] = []
    prior: list[tuple] = []
    for t, outcome, wallet, usd, price in rows:
        if str(outcome).casefold().strip() not in names:
            continue
        if not 0 < float(price) <= 1 or float(usd) <= 0:
            continue
        if not now - BASELINE_SECONDS <= int(t) <= now + 5:
            continue
        value = (names[str(outcome).casefold().strip()], str(wallet or ""), float(usd), float(price))
        if int(t) >= now - WINDOW_SECONDS:
            recent.append(value)
        else:
            prior.append(value)
    if len(recent) < 3:
        return []
    total = sum(r[2] for r in recent)
    if total < min_buy_usd:
        return []

    previous_total = sum(r[2] for r in prior)
    baseline_minutes = (BASELINE_SECONDS - WINDOW_SECONDS) / 60.0
    comparison_available = previous_total >= baseline_min_usd
    multiplier = (total / (previous_total * (WINDOW_SECONDS / (BASELINE_SECONDS - WINDOW_SECONDS)))
                  if comparison_available else None)
    by_side = defaultdict(list)
    by_prior_side = defaultdict(float)
    for row in recent:
        by_side[row[0]].append(row)
    for row in prior:
        by_prior_side[row[0]] += row[2]

    signals = []
    for name in names.values():
        side_rows = by_side[name]
        if not side_rows:
            continue
        amount = sum(row[2] for row in side_rows)
        share = amount / total
        wallets = defaultdict(float)
        for _, wallet, usd, _ in side_rows:
            if wallet:
                wallets[wallet] += usd
        wallet_count = len(wallets)
        top_wallet_share = max(wallets.values(), default=0.0) / amount
        earlier_share = by_prior_side[name] / previous_total if comparison_available else None
        delta_pp = (share - earlier_share) * 100 if earlier_share is not None else None
        imbalanced = share >= min_share and amount >= min_buy_usd
        burst = multiplier is not None and multiplier >= burst_multiplier and share >= 0.70
        if not (imbalanced or burst):
            continue
        # One whale is not a multi-wallet market pressure signal. Dedicated
        # priority purchase alerts already handle those large individual buys.
        if wallet_count < minimum_wallets or top_wallet_share > 0.85:
            continue
        score = min(100, round(
            36 + (share - .5) * 60 + min(18, log10(max(1, total) / 1000) * 13)
            + (min(15, (multiplier - 1) * 2.5) if multiplier is not None else 0)
            + (min(9, max(0, delta_pp) / 4) if delta_pp is not None else 0)
            + min(7, wallet_count - minimum_wallets)
        ))
        urgent = (amount >= 8000 and share >= 0.88 and wallet_count >= 4
                  and (multiplier is not None and multiplier >= 5
                       or delta_pp is not None and delta_pp >= 25))
        signals.append({
            "outcome": name, "amount": amount, "total": total, "share": share,
            "buys": len(side_rows), "market_buys": len(recent),
            "wallets": wallet_count, "top_wallet_share": top_wallet_share,
            "avg_odds": sum(r[2] / r[3] for r in side_rows) / amount,
            "prior_amount": previous_total, "prior_share": earlier_share,
            "delta_pp": delta_pp, "burst_ratio": multiplier,
            "burst": burst, "imbalanced": imbalanced, "urgent": urgent,
            "score": score, "window_minutes": WINDOW_SECONDS // 60,
            "baseline_minutes": int(baseline_minutes),
        })
    return sorted(signals, key=lambda x: (-x["score"], -x["amount"]))
