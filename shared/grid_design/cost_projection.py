"""Component cost projection — Python port of the BoS Purchases sheet's
`BATCH_GET_PROJECTIONS` Apps Script, plus the Avg Cost List weighted-DDP average.

For each item (matched by name to a component), it analyses the purchase history:
  * weighted DDP = Σ(unit_usd × qty) / Σ(qty)          (the "Weighted DDP cost")
  * projected   = latest_price × (1 + time-decayed annual inflation)
…and writes ddp_cost / projected_cost (+ confidence, num_purchases, projected_at)
onto gd_components. Called just-in-time at BOM generation, so each generated BOM is
priced off the current ledger.

Algorithm (faithful to the source):
  * prices normalised to USD (divide by exchange rate when currency != "USD")
  * annual rate = (latest − first) / first / yearsElapsed (needs >0.08 yr span)
  * < 3 purchases -> clamp rate to [-10%, +20%]
  * downward-trend guard: final multiplier floored at -5%
  * confidence: High (3+ buys & <120d) · Med (2+ & <200d) · Low otherwise
"""

from __future__ import annotations

from datetime import datetime, timezone

from shared.grid_design.data import load, num
from shared.grid_design.db import Repository

_YEAR_DAYS = 365.25


def project_costs(
    purchases: list[dict], exchange_rate: float, now: datetime | None = None
) -> dict[str, dict]:
    """Pure core. `purchases` = [{date: datetime, item: str, currency: str,
    unit_price: float, qty: float}, …]. Returns {item_name: {...}}."""

    def _naive(d: datetime) -> datetime:
        return d.replace(tzinfo=None) if d.tzinfo else d

    now = _naive(now or datetime.now(timezone.utc))

    by_item: dict[str, list[dict]] = {}
    for p in purchases:
        name = p.get("item")
        date = p.get("date")
        unit = num(p.get("unit_price"))
        if not name or unit <= 0 or not isinstance(date, datetime):
            continue
        price = unit / exchange_rate if (p.get("currency") != "USD" and exchange_rate > 0) else unit
        by_item.setdefault(name, []).append(
            {"date": _naive(date), "price": price, "qty": num(p.get("qty"))}
        )

    out: dict[str, dict] = {}
    for name, hist in by_item.items():
        hist.sort(key=lambda h: h["date"])
        latest, first = hist[-1], hist[0]

        annual_rate = 0.05
        if len(hist) > 1:
            years = (latest["date"] - first["date"]).total_seconds() / (_YEAR_DAYS * 86400)
            if years > 0.08 and first["price"]:
                annual_rate = (latest["price"] - first["price"]) / first["price"] / years
        if len(hist) < 3:
            annual_rate = min(max(annual_rate, -0.10), 0.20)

        days_since = max(0, (now - latest["date"]).days)
        raw_mult = annual_rate * (days_since / _YEAR_DAYS)
        safe_mult = max(raw_mult, -0.05)
        projected = latest["price"] * (1 + safe_mult)

        confidence = (
            "High"
            if (len(hist) >= 3 and days_since < 120)
            else "Med"
            if (len(hist) >= 2 and days_since < 200)
            else "Low"
        )

        total_qty = sum(h["qty"] for h in hist)
        weighted_ddp = (
            (sum(h["price"] * h["qty"] for h in hist) / total_qty) if total_qty else latest["price"]
        )

        out[name] = {
            "ddp_cost": round(weighted_ddp, 6),
            "projected_cost": round(projected, 6),
            "num_purchases": len(hist),
            "days_since_last": days_since,
            "confidence": f"{confidence} ({annual_rate * 100:.1f}%)",
        }
    return out


def recompute_component_costs(exchange_rate: float | None = None) -> dict:
    """Recompute projections from gd_purchases and write them onto gd_components.

    Matches purchases to components by name. Returns a summary dict.
    """
    from shared.grid_design.exchange_rate import get_usd_to_ngn

    rate = exchange_rate or get_usd_to_ngn() or 0.0
    purchase_rows = load("purchases", active_only=True)

    purchases = []
    for r in purchase_rows:
        date = r.get("date")
        if isinstance(date, str) and date:
            try:
                date = datetime.fromisoformat(date.replace("Z", "+00:00"))
            except ValueError:
                date = None
        purchases.append(
            {
                "date": date,
                "item": r.get("item_description"),
                "currency": r.get("currency"),
                "unit_price": r.get("landed_unit_cost_usd"),
                "qty": r.get("qty"),
            }
        )

    projections = project_costs(purchases, rate)

    components = load("components")
    now_iso = datetime.now(timezone.utc).isoformat()
    updates = []
    matched = 0
    for c in components:
        proj = projections.get(c.get("name"))
        if not proj:
            continue
        matched += 1
        updates.append(
            {
                "id": c["id"],
                "ddp_cost": proj["ddp_cost"],
                "projected_cost": proj["projected_cost"],
                "num_purchases": proj["num_purchases"],
                "cost_confidence": proj["confidence"],
                "cost_projected_at": now_iso,
            }
        )

    repo = Repository("components")
    for i in range(0, len(updates), 500):
        repo.upsert(updates[i : i + 500])

    return {
        "ok": True,
        "items_with_history": len(projections),
        "components_updated": matched,
        "exchange_rate": rate,
    }
