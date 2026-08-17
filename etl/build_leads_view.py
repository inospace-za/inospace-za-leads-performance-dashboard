"""
Transform _incoming/data.json + _incoming/history.json (+ optional
_incoming/historical_monthly.json), all fetched from the source repo, into
site/leads_data.json: a CBD-only-by-default, leads-performance-focused view
scored against agreed KPI bands, with a rolling 3-month history per site.

Schema confirmed against real samples (2026-08-14 / 2026-08-17):
  data["by_site"][site_id]["leads"]        -> placed_total, converted_to_lease, conversion_pct (0-100)
  data["by_site"][site_id]["moveins"/"moveouts"/"net_moves"]
  data["by_site"][site_id]["occ_pct_units"/"occ_pct_area"]  -> percentages 0-100, NOT fractions
  data["sites"]                             -> list of {id, name, code, city} (metadata only)
  history.json                              -> top-level list of daily snapshots:
    {date, portfolio_occ_pct, portfolio_units_occupied, by_site_occ_pct: {site_id: pct},
     leads_placed, leases_signed, net_moves}
  historical_monthly.json (optional, one-time backfill)   -> {
     months: [...], by_site: {site_id: {"YYYY-MM": {moveins, moveouts, transfers,
     leads_placed, leases_signed, cancelled}}}
  }

Known, real gaps in the source data (not fabricated around):
  - Per-site history is ONLY reliable for occupancy % (by_site_occ_pct, unit-based)
    from 2026-07-02 onward (when the daily pipeline started).
  - historical_monthly.json covers real per-site leads/moves for Jan-Jun 2026, but
    has NO occupancy figures at all.
  - There is a real gap for any month that is in neither source (e.g. per-site leads
    for July 2026): those cells are left as null / rendered as "-".

Usage:
    python etl/build_leads_view.py
"""
import json
import os
from datetime import date

INCOMING_DIR = "_incoming"
OUT_PATH = "docs/leads_data.json"

CBD_SITE_CODE = "58995"  # site id for CBD / The Exchange (sLocationCode L004)

# All sites the source pipeline tracks, keyed by numeric site id (matches data["by_site"] keys).
KNOWN_SITES = {
    "56788": "Maitland",
    "58700": "Salt River",
    "58995": "CBD - The Exchange",
}

MONTHS_OF_HISTORY = 3

# ---------------------------------------------------------------------------
# KPI bands - editable. Each list is (min_threshold, label), ascending.
# ---------------------------------------------------------------------------
BANDS = {
    "genuine_enquiries": [
        (0, "Concerning - insufficient demand generation"),
        (40, "Acceptable - needs improvement"),
        (60, "Good"),
        (80, "Very Good"),
    ],
    "conversion_rate": [
        (0.00, "Concerning"),
        (0.20, "Below Target"),
        (0.25, "Acceptable"),
        (0.30, "Good"),
        (0.35, "Very Good"),
        (0.40, "Excellent"),
    ],
    "move_ins": [
        (0, "Concerning"),
        (10, "Reasonable"),
        (15, "Good"),
        (20, "Very Good"),
    ],
    "occupancy_sqm": [
        (0.00, "Concerning"),
        (0.20, "Reasonable"),
        (0.30, "Good"),
        (0.40, "Very Good"),
        (0.50, "Exceptional"),
    ],
}

DIAGNOSTIC_THRESHOLDS = {
    "low_demand_min": 20,
    "low_demand_max": 30,
    "price_issue_enq_min": 70,
    "price_issue_enq_max": 90,
    "price_issue_moveins_max": 15,
}


def band_label(value, band_key):
    if value is None:
        return None
    rows = BANDS[band_key]
    label = rows[0][1]
    for threshold, lbl in rows:
        if value >= threshold:
            label = lbl
        else:
            break
    return label


def diagnostic_flags(genuine_enquiries, move_ins):
    flags = []
    if genuine_enquiries is None:
        return flags
    t = DIAGNOSTIC_THRESHOLDS
    if t["low_demand_min"] <= genuine_enquiries <= t["low_demand_max"]:
        flags.append("Insufficient demand generation")
    if (
        t["price_issue_enq_min"] <= genuine_enquiries <= t["price_issue_enq_max"]
        and move_ins is not None
        and move_ins < t["price_issue_moveins_max"]
    ):
        flags.append("Possible price / product / sales issue")
    return flags


def load_incoming():
    with open(os.path.join(INCOMING_DIR, "data.json")) as f:
        data = json.load(f)
    with open(os.path.join(INCOMING_DIR, "history.json")) as f:
        history = json.load(f)
    historical_monthly = None
    hm_path = os.path.join(INCOMING_DIR, "historical_monthly.json")
    if os.path.exists(hm_path):
        with open(hm_path) as f:
            historical_monthly = json.load(f)
    return data, history, historical_monthly


def extract_site_current_month(data, site_code):
    site = data.get("by_site", {}).get(site_code, {})
    leads = site.get("leads", {})

    enquiries = leads.get("placed_total")
    conversions = leads.get("converted_to_lease")
    move_ins = site.get("moveins")
    move_outs = site.get("moveouts")
    net_moves = site.get("net_moves")

    conversion_pct = leads.get("conversion_pct")
    occ_pct_units = site.get("occ_pct_units")
    occ_pct_area = site.get("occ_pct_area")

    conversion_rate = (conversion_pct / 100) if conversion_pct is not None else None
    occupancy_sqm_pct = (occ_pct_area / 100) if occ_pct_area is not None else None
    occupancy_units_pct = (occ_pct_units / 100) if occ_pct_units is not None else None

    net_units_absorbed = (
        net_moves if net_moves is not None
        else (move_ins - move_outs) if move_ins is not None and move_outs is not None
        else None
    )

    return {
        "genuine_enquiries": enquiries,
        "conversions": conversions,
        "conversion_rate": conversion_rate,
        "move_ins": move_ins,
        "move_outs": move_outs,
        "net_units_absorbed": net_units_absorbed,
        "occupancy_sqm_pct": occupancy_sqm_pct,
        "occupancy_units_pct": occupancy_units_pct,
    }


def _daily_occupancy_by_month(history, site_code):
    """Average by_site_occ_pct (unit-based, 0-100) per calendar month from the
    daily-accumulating history.json. Returns {"YYYY-MM": avg_pct_0to100}."""
    buckets = {}
    for day in history:
        d = day.get("date")
        if not d:
            continue
        occ = (day.get("by_site_occ_pct") or {}).get(site_code)
        if occ is None:
            continue
        month = d[:7]
        buckets.setdefault(month, []).append(occ)
    return {m: sum(v) / len(v) for m, v in buckets.items()}


def _recent_month_keys(run_date_str, n):
    """Last n calendar months as 'YYYY-MM' strings, oldest first, ending at
    the month of run_date_str (or today if not parseable)."""
    try:
        y, m, _ = [int(x) for x in run_date_str.split("-")]
        anchor = date(y, m, 1)
    except Exception:
        today = date.today()
        anchor = date(today.year, today.month, 1)
    keys = []
    y, m = anchor.year, anchor.month
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(keys))


def extract_site_recent_months(data, history, historical_monthly, site_code, current_month_data, run_date_str):
    """
    Builds the last MONTHS_OF_HISTORY calendar months for one site, merging:
      - the live current month (real, from data.json) for the most recent month
      - historical_monthly.json (real leads/moves, Jan-Jun 2026, no occupancy) where available
      - daily history.json's averaged occupancy % where available
    Cells with no real source are left as None (rendered as "-"), never fabricated.
    """
    month_keys = _recent_month_keys(run_date_str, MONTHS_OF_HISTORY)
    current_month_key = month_keys[-1]
    daily_occ_by_month = _daily_occupancy_by_month(history, site_code)
    hm_by_month = ((historical_monthly or {}).get("by_site", {}) or {}).get(site_code, {})

    rows = []
    for month in month_keys:
        if month == current_month_key:
            row = {
                "month": month,
                "genuine_enquiries": current_month_data["genuine_enquiries"],
                "conversions": current_month_data["conversions"],
                "conversion_rate": current_month_data["conversion_rate"],
                "move_ins": current_month_data["move_ins"],
                "move_outs": current_month_data["move_outs"],
                "occupancy_sqm_pct": current_month_data["occupancy_sqm_pct"],
                "occupancy_units_pct": current_month_data["occupancy_units_pct"],
                "source": "live_current_month",
            }
        else:
            hm = hm_by_month.get(month)
            enquiries = hm.get("leads_placed") if hm else None
            conversions = hm.get("leases_signed") if hm else None
            move_ins = hm.get("moveins") if hm else None
            move_outs = hm.get("moveouts") if hm else None
            conversion_rate = (
                (conversions / enquiries) if enquiries else None
            ) if (enquiries is not None and conversions is not None) else None

            avg_occ = daily_occ_by_month.get(month)
            occupancy_units_pct = (avg_occ / 100) if avg_occ is not None else None

            sources = []
            if hm:
                sources.append("historical_monthly_backfill")
            if avg_occ is not None:
                sources.append("daily_history_occupancy_avg")

            row = {
                "month": month,
                "genuine_enquiries": enquiries,
                "conversions": conversions,
                "conversion_rate": conversion_rate,
                "move_ins": move_ins,
                "move_outs": move_outs,
                "occupancy_sqm_pct": None,  # never available historically, only units-based is
                "occupancy_units_pct": occupancy_units_pct,
                "source": "+".join(sources) if sources else "no_data",
            }
        rows.append(row)
    return rows


def build_site(data, history, historical_monthly, site_code, site_name, run_date_str):
    current = extract_site_current_month(data, site_code)
    monthly_history = extract_site_recent_months(
        data, history, historical_monthly, site_code, current, run_date_str
    )

    current["bands"] = {
        "genuine_enquiries": band_label(current["genuine_enquiries"], "genuine_enquiries"),
        "conversion_rate": band_label(current["conversion_rate"], "conversion_rate"),
        "move_ins": band_label(current["move_ins"], "move_ins"),
        "occupancy_sqm": band_label(current["occupancy_sqm_pct"], "occupancy_sqm"),
    }
    current["diagnostic_flags"] = diagnostic_flags(current["genuine_enquiries"], current["move_ins"])

    for m in monthly_history:
        m["bands"] = {
            "genuine_enquiries": band_label(m.get("genuine_enquiries"), "genuine_enquiries"),
            "conversion_rate": band_label(m.get("conversion_rate"), "conversion_rate"),
            "move_ins": band_label(m.get("move_ins"), "move_ins"),
            "occupancy_sqm": band_label(m.get("occupancy_sqm_pct"), "occupancy_sqm"),
        }

    return {
        "name": site_name,
        "site_code": site_code,
        "current_month": current,
        "monthly_history": monthly_history,
    }


def build():
    data, history, historical_monthly = load_incoming()
    run_date_str = (data.get("meta") or {}).get("run_date", "")

    sites_out = {
        code: build_site(data, history, historical_monthly, code, name, run_date_str)
        for code, name in KNOWN_SITES.items()
    }

    out = {
        "generated_note": "Built by leads-performance-dashboard/etl/build_leads_view.py",
        "generated_at": (data.get("meta") or {}).get("generated_at"),
        "run_date": run_date_str,
        "sites": sites_out,
        "default_site": CBD_SITE_CODE,
        "bands_reference": BANDS,
        "diagnostic_thresholds": DIAGNOSTIC_THRESHOLDS,
        "bands_note": "Same KPI bands apply to every site initially - split per site later if needed.",
        "history_note": (
            "Monthly history blends three sources: the live current month (real), "
            "a one-time Jan-Jun 2026 backfill of real per-site leads/moves (no occupancy "
            "in that source), and the daily pipeline's per-site occupancy average from "
            "2026-07-02 onward (no per-site leads in that source). Cells with no real "
            "source are left blank rather than estimated."
        ),
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    build()
