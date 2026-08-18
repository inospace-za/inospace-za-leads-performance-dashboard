"""
Transform _incoming/data.json (fetched from the source repo) into
docs/leads_data.json: a CBD-only-by-default, leads-performance-focused view
scored against agreed KPI bands.

Monthly history (2026-08-18 onward): tracked via a self-accumulating, committed
archive at docs/leads_history_archive.json - NOT backfilled or blended from any
historical source. Every day's run upserts the CURRENT month's entry in the
archive with the latest month-to-date figures; once the month closes and a new
current month begins, that entry is never touched again and becomes the real,
final historical value for that month. History starts empty and grows by
exactly one real month per site as time passes - no fabrication, no guessing,
no blending mismatched sources. (An earlier version tried backfilling May-Jul
2026 from a manually-built file and separately from the source repo's own
historical_monthly.json; the two disagreed for at least one site/month, so
both were dropped in favour of this simpler, fully-real approach.)

Schema confirmed against real samples (2026-08-14 / 2026-08-18):
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
    "occupancy_sqm_pct" so the two headline metrics remain directly comparable.

"leads" (placed_inquiries) and "genuine_enquiries" (placed_reservations) are two
distinct metrics as of 2026-08-18 - previously a single combined "genuine_enquiries"
(placed_total) figure.

Usage:
    python etl/build_leads_view.py
"""
import json
import os
from datetime import date

INCOMING_DIR = "_incoming"
OUT_PATH = "docs/leads_data.json"
ARCHIVE_PATH = "docs/leads_history_archive.json"  # self-accumulating, committed monthly
                                                    # archive - see load_archive()/
                                                    # update_archive_for_site() below

CBD_SITE_CODE = "58995"  # site id for CBD / The Exchange (sLocationCode L004)

# All sites the source pipeline tracks, keyed by numeric site id (matches data["by_site"] keys).
KNOWN_SITES = {
    "56788": "Maitland",
    "58700": "Salt River",
    "58995": "CBD - The Exchange",
}

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
    return data


def extract_site_current_month(data, site_code):
    site = data.get("by_site", {}).get(site_code, {})
    leads = site.get("leads", {})

    # 2026-08-18: split what used to be one combined "genuine_enquiries" figure into
    # two distinct metrics, per direct clarification of the real business definitions:
    #   leads              = placed_total         (ALL new inquiries - every contact,
    #                        vetted or not, is a lead)
    #   genuine_enquiries  = placed_reservations   (the subset the team has vetted and
    #                        confirmed has real intent/commitment to rent - i.e. once a
    #                        lead is confirmed genuine, SiteLink records it as a
    #                        reservation, hence placed_reservations is the right field)
    # Only available for the live current month - the source's historical backfill only
    # ever has one combined "leads_placed" figure, never a real inquiries/reservations
    # split, so historical months leave both of these blank rather than guessing which
    # one the combined number represents.
    leads_count = leads.get("placed_total")
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


def load_archive():
    """The self-accumulating monthly archive - see module docstring. Starts empty;
    grows by exactly one real entry per site per calendar month, going forward
    from 2026-08-18."""
    if os.path.exists(ARCHIVE_PATH):
        with open(ARCHIVE_PATH) as f:
            return json.load(f)
    return {}


def save_archive(archive):
    os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
    with open(ARCHIVE_PATH, "w") as f:
        json.dump(archive, f, indent=2)


def update_archive_for_site(archive, site_code, current_month_key, current_month_data):
    """Upserts today's live current-month figures into the archive under the
    current month's key. Run daily: every day's run overwrites that SAME month's
    entry with the latest month-to-date figures, so the entry naturally settles
    on its final value once the month closes and a new current_month_key begins -
    no separate "close out the month" step needed. Past months' entries are never
    touched again once the key moves on, so they stay frozen at their real,
    actually-observed final value."""
    site_archive = archive.setdefault(site_code, {})
    site_archive[current_month_key] = {
        "leads": current_month_data["leads"],
        "genuine_enquiries": current_month_data["genuine_enquiries"],
        "conversions": current_month_data["conversions"],
        "conversion_rate": current_month_data["conversion_rate"],
        "move_ins": current_month_data["move_ins"],
        "move_outs": current_month_data["move_outs"],
        "occupancy_sqm_pct": current_month_data["occupancy_sqm_pct"],
        "occupancy_units_pct": current_month_data["occupancy_units_pct"],
        "occupancy_units_rentable_pct": current_month_data["occupancy_units_rentable_pct"],
    }


def monthly_history_from_archive(archive, site_code, current_month_key, max_months=12):
    """Real monthly history is whatever has actually accumulated in the archive
    for this site, oldest first, capped at max_months. No backfill, no blending,
    no fabrication - this only ever contains months this pipeline has itself
    observed since it started tracking (2026-08-18 onward)."""
    site_archive = archive.get(site_code, {})
    months = sorted(site_archive.keys())[-max_months:]
    rows = []
    for month in months:
        row = dict(site_archive[month])
        row["month"] = month
        row["source"] = "live_current_month" if month == current_month_key else "tracked_archive"
        rows.append(row)
    return rows


def build_site(archive, site_code, site_name, current_month_key):
    current = archive[site_code][current_month_key].copy()
    monthly_history = monthly_history_from_archive(archive, site_code, current_month_key)

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
    data = load_incoming()
    run_date_str = (data.get("meta") or {}).get("run_date", "")
    try:
        current_month_key = "-".join(run_date_str.split("-")[:2])
    except Exception:
        today = date.today()
        current_month_key = f"{today.year:04d}-{today.month:02d}"

    archive = load_archive()
    for code in KNOWN_SITES:
        current = extract_site_current_month(data, code)
        update_archive_for_site(archive, code, current_month_key, current)
    save_archive(archive)

    sites_out = {
        code: build_site(archive, code, name, current_month_key)
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
            "Monthly history is tracked from 2026-08-18 onward only - no backfilled or "
            "blended historical data. Each site's history grows by one real month at a "
            "time as the daily pipeline runs: every day's run updates the current month's "
            "entry in the committed archive (docs/leads_history_archive.json) with the "
            "latest month-to-date figures, and that entry is never touched again once the "
            "month closes and a new one begins. Months before tracking started simply "
            "aren't in the history - nothing is backfilled or estimated."
        ),
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    build()
