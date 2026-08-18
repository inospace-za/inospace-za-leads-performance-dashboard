"""
Transform _incoming/data.json + _incoming/history.json (+ optional
_incoming/historical_monthly.json), all fetched from the source repo, into
site/leads_data.json: a CBD-only-by-default, leads-performance-focused view
scored against agreed KPI bands, with a rolling 3-month history per site.

Schema confirmed against real samples (2026-08-14 / 2026-08-17):
  data["by_site"][site_id]["leads"]        -> placed_inquiries, placed_reservations, placed_total,
                                               converted_to_lease, conversion_pct (0-100)
  data["by_site"][site_id]["moveins"/"moveouts"/"net_moves"]
  data["by_site"][site_id]["occ_pct_units"/"occ_pct_area"]  -> percentages 0-100, NOT fractions
  data["sites"]                             -> list of {id, name, code, city} (metadata only)
  data["categories"][site_id]               -> confirmed 2026-08-18 against
    sitelink-analytics-dashboard/etl/parse_reports.py (parse_occupancy_stats()):
    list of per-unit-type dicts {type, occupied, vacant, unrentable, total, occ_pct,
    avg_std_rate, gross_potential, gross_occupied}. Used only to compute the
    SUPPLEMENTARY "occupancy_units_rentable_pct" figure (see _rentable_occ_pct_units()
    below) - the headline "occupancy_units_pct" stays on the same all-units basis as
    "occupancy_sqm_pct" so the two headline metrics remain directly comparable
    (per 2026-08-18 discussion: reported units%/sqm% must share the same basis).
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
  - The inquiries-vs-reservations split (placed_inquiries/placed_reservations) is only
    available for the live current month. Neither historical_monthly.json nor
    history.json (yet) carries this split, so historical rows leave it null.

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

MONTHS_OF_HISTORY = 4  # widened 2026-08-18 to bring May 2026 into view alongside Jun/Jul/Aug

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


def _rentable_occ_pct_units(categories_for_site):
    """Occupancy % excluding unrentable stock from the denominator.

    data["categories"][site_code] is a list of per-unit-type dicts from
    parse_occupancy_stats() in sitelink-analytics-dashboard/etl/parse_reports.py
    (confirmed against the real source, 2026-08-18), each shaped:
      {"type", "occupied", "vacant", "unrentable", "total", "occ_pct", ...}
    That source's own "occ_pct" divides occupied by "total", which INCLUDES
    unrentable units in the denominator. This recomputes it divided by
    (total - unrentable) instead, so offline/unrentable stock doesn't drag
    the occupancy figure down. Returns a 0-100 percentage, or None if there's
    no rentable stock to divide by (or no categories data for this site).
    """
    if not categories_for_site:
        return None
    occupied = sum(row.get("occupied", 0) or 0 for row in categories_for_site)
    unrentable = sum(row.get("unrentable", 0) or 0 for row in categories_for_site)
    total = sum(row.get("total", 0) or 0 for row in categories_for_site)
    rentable_total = total - unrentable
    if rentable_total <= 0:
        return None
    return round(100 * occupied / rentable_total, 1)


def load_incoming():
    with open(os.path.join(INCOMING_DIR, "data.json")) as f:
        data = json.load(f)
    with open(os.path.join(INCOMING_DIR, "history.json")) as f:
        history = json.load(f)
    historical_monthly = None
    hm_path = os.path.join(INCOMING_DIR, "historical_monthly.json")
    # historical_monthly.json, when present, is fetched by fetch_source_data.py from
    # sitelink-analytics-dashboard's own site/historical_monthly.json (the source
    # pipeline's official one-time backfill). That is treated as authoritative -
    # no local override. (2026-08-18: a manually-built data/historical_monthly.json
    # was tried here as a fallback and found to disagree with the official backfill
    # for at least one site/month; removed in favour of trusting the source.)
    if os.path.exists(hm_path):
        with open(hm_path) as f:
            historical_monthly = json.load(f)
    return data, history, historical_monthly


def extract_site_current_month(data, site_code):
    site = data.get("by_site", {}).get(site_code, {})
    leads = site.get("leads", {})

    # 2026-08-18: split what used to be one combined "genuine_enquiries" (placed_total)
    # into two distinct metrics per direct instruction:
    #   leads              = placed_inquiries    (first-contact enquiries)
    #   genuine_enquiries  = placed_reservations  (people who've committed to reserving -
    #                        the more qualified/"genuine" signal)
    # Only available for the live current month - the source's historical backfill only
    # ever has one combined "leads_placed" figure, never a real inquiries/reservations
    # split, so historical months leave both of these blank rather than guessing which
    # one the combined number represents.
    leads_count = leads.get("placed_inquiries")
    enquiries = leads.get("placed_reservations")
    conversions = leads.get("converted_to_lease")
    move_ins = site.get("moveins")
    move_outs = site.get("moveouts")
    net_moves = site.get("net_moves")

    conversion_pct = leads.get("conversion_pct")
    occ_pct_units = site.get("occ_pct_units")
    occ_pct_area = site.get("occ_pct_area")

    conversion_rate = (conversion_pct / 100) if conversion_pct is not None else None
    occupancy_sqm_pct = (occ_pct_area / 100) if occ_pct_area is not None else None

    # occupancy_units_pct is on the SAME basis as occupancy_sqm_pct: % of ALL units
    # (unrentable included in the denominator) - so the two headline occupancy figures
    # stay directly comparable, per 2026-08-18 discussion.
    occupancy_units_pct = (occ_pct_units / 100) if occ_pct_units is not None else None

    # Supplementary figure only: % of RENTABLE units (excludes offline/unrentable stock
    # from the denominator), from data["categories"][site_code]. Shown as a sub-line
    # under the headline metric, not used for banding/scoring.
    categories_for_site = data.get("categories", {}).get(site_code)
    rentable_occ_pct = _rentable_occ_pct_units(categories_for_site)
    occupancy_units_rentable_pct = (rentable_occ_pct / 100) if rentable_occ_pct is not None else None

    net_units_absorbed = (
        net_moves if net_moves is not None
        else (move_ins - move_outs) if move_ins is not None and move_outs is not None
        else None
    )

    return {
        "leads": leads_count,
        "genuine_enquiries": enquiries,
        "conversions": conversions,
        "conversion_rate": conversion_rate,
        "move_ins": move_ins,
        "move_outs": move_outs,
        "net_units_absorbed": net_units_absorbed,
        "occupancy_sqm_pct": occupancy_sqm_pct,
        "occupancy_units_pct": occupancy_units_pct,
        "occupancy_units_rentable_pct": occupancy_units_rentable_pct,
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
                "leads": current_month_data["leads"],
                "genuine_enquiries": current_month_data["genuine_enquiries"],
                "conversions": current_month_data["conversions"],
                "conversion_rate": current_month_data["conversion_rate"],
                "move_ins": current_month_data["move_ins"],
                "move_outs": current_month_data["move_outs"],
                "occupancy_sqm_pct": current_month_data["occupancy_sqm_pct"],
                "occupancy_units_pct": current_month_data["occupancy_units_pct"],
                "occupancy_units_rentable_pct": current_month_data["occupancy_units_rentable_pct"],
                "source": "live_current_month",
            }
        else:
            hm = hm_by_month.get(month)
            # hm's "leads_placed" is a single COMBINED figure (inquiries + reservations) -
            # the source's historical backfill has never carried a real inquiries-vs-
            # reservations split. Since 2026-08-18 "leads" and "genuine_enquiries" are two
            # distinct metrics (placed_inquiries vs placed_reservations respectively), and
            # attributing the combined historical number to either would misrepresent it -
            # so both are left blank for historical months. The combined figure is still
            # used for conversion_rate below, since that ratio (leases signed vs total lead
            # activity) remains a meaningful, real number on its own.
            combined_placed = hm.get("leads_placed") if hm else None
            conversions = hm.get("leases_signed") if hm else None
            move_ins = hm.get("moveins") if hm else None
            move_outs = hm.get("moveouts") if hm else None
            conversion_rate = (
                (conversions / combined_placed) if combined_placed else None
            ) if (combined_placed is not None and conversions is not None) else None

            avg_occ = daily_occ_by_month.get(month)
            occupancy_units_pct = (avg_occ / 100) if avg_occ is not None else None

            sources = []
            if hm:
                sources.append("historical_monthly_backfill")
            if avg_occ is not None:
                sources.append("daily_history_occupancy_avg")

            row = {
                "month": month,
                "leads": None,  # see comment above - no real split available historically
                "genuine_enquiries": None,
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
        # Reuses the occupancy_sqm thresholds - same bands, different denominator (units, not sqm).
        "occupancy_units": band_label(current["occupancy_units_pct"], "occupancy_sqm"),
    }
    current["diagnostic_flags"] = diagnostic_flags(current["genuine_enquiries"], current["move_ins"])

    for m in monthly_history:
        m["bands"] = {
            "genuine_enquiries": band_label(m.get("genuine_enquiries"), "genuine_enquiries"),
            "conversion_rate": band_label(m.get("conversion_rate"), "conversion_rate"),
            "move_ins": band_label(m.get("move_ins"), "move_ins"),
            "occupancy_sqm": band_label(m.get("occupancy_sqm_pct"), "occupancy_sqm"),
            "occupancy_units": band_label(m.get("occupancy_units_pct"), "occupancy_sqm"),
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
