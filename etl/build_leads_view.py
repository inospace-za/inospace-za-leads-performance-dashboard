"""
Transform _incoming/data.json + _incoming/history.json (fetched from the source repo)
into site/leads_data.json: a CBD-only, leads-performance-focused view scored against
agreed KPI bands.

*** SCHEMA NOT YET CONFIRMED ***
Every place marked TODO reads a *guessed* field name based on the source repo's README
description, not a real sample file. Send a real data.json/history.json and these will
get corrected in one pass - the band/scoring logic below does not need to change.

Usage:
    python etl/build_leads_view.py
"""
import json
import os

INCOMING_DIR = "_incoming"
OUT_PATH = "docs/leads_data.json"

CBD_SITE_CODE = "58995"   # per source README: CBD/58995 (a.k.a. sLocationCode L004)

# All sites the source pipeline tracks (per its README KNOWN_SITES). Same KPI bands
# apply to every site initially - split thresholds per site later if needed.
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


def load_incoming():
    with open(os.path.join(INCOMING_DIR, "data.json")) as f:
        data = json.load(f)
    with open(os.path.join(INCOMING_DIR, "history.json")) as f:
        history = json.load(f)
    return data, history


def extract_site_current_month(data, site_code):
    """
    TODO: confirm real field names. Expected (per source README) to come from the
    Consolidated Lead Funnel (month-to-date) and Move-Ins & Move-Outs (month-to-date)
    reports, filtered/grouped to the given site.

    Guessed shape - CORRECT ONCE A REAL data.json IS AVAILABLE:
        data["sites"][site_code]["lead_funnel"]["enquiries"]
        data["sites"][site_code]["lead_funnel"]["conversions"]
        data["sites"][site_code]["moves"]["move_ins"]
        data["sites"][site_code]["moves"]["move_outs"]
        data["sites"][site_code]["occupancy"]["occupied_sqm"]
        data["sites"][site_code]["occupancy"]["total_sqm"]
        data["sites"][site_code]["occupancy"]["occupied_units"]
        data["sites"][site_code]["occupancy"]["total_units"]
    """
    site = data.get("sites", {}).get(site_code, {})  # TODO confirm key path
    lead_funnel = site.get("lead_funnel", {})
    moves = site.get("moves", {})
    occ = site.get("occupancy", {})

    enquiries = lead_funnel.get("enquiries")      # TODO confirm field name
    conversions = lead_funnel.get("conversions")  # TODO confirm field name
    move_ins = moves.get("move_ins")              # TODO confirm field name
    move_outs = moves.get("move_outs")            # TODO confirm field name
    occupied_sqm = occ.get("occupied_sqm")        # TODO confirm field name
    total_sqm = occ.get("total_sqm")              # TODO confirm field name
    occupied_units = occ.get("occupied_units")    # TODO confirm field name
    total_units = occ.get("total_units")           # TODO confirm field name

    conversion_rate = (
        conversions / enquiries if enquiries and conversions is not None else None
    )
    occupancy_sqm_pct = occupied_sqm / total_sqm if occupied_sqm is not None and total_sqm else None
    occupancy_units_pct = (
        occupied_units / total_units if occupied_units is not None and total_units else None
    )

    return {
        "genuine_enquiries": enquiries,
        "conversions": conversions,
        "conversion_rate": conversion_rate,
        "move_ins": move_ins,
        "move_outs": move_outs,
        "net_units_absorbed": (move_ins - move_outs) if move_ins is not None and move_outs is not None else None,
        "occupancy_sqm_pct": occupancy_sqm_pct,
        "occupancy_units_pct": occupancy_units_pct,
    }


def extract_site_monthly_history(history, site_code):
    """
    TODO: confirm whether history.json keeps genuinely per-site leads history, or
    only portfolio-blended figures (source README flags this exact limitation on
    its own Lead Velocity panel). Occupied History / Move-Ins & Move-Outs ARE
    expected to be per-site and reliable; leads month-over-month per site may only
    be as long as this pipeline has been running.

    Returns a list of {month, genuine_enquiries, conversions, move_ins, move_outs,
    occupancy_sqm_pct, occupancy_units_pct} dicts, oldest first.
    """
    months = []
    # TODO: replace with real iteration once schema is confirmed, e.g.:
    # for day in history.get("daily", []):
    #     ... bucket by month, filter to site_code ...
    return months


def build_site(data, history, site_code, site_name):
    current = extract_site_current_month(data, site_code)
    monthly_history = extract_site_monthly_history(history, site_code)

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
    data, history = load_incoming()

    sites_out = {
        code: build_site(data, history, code, name)
        for code, name in KNOWN_SITES.items()
    }

    out = {
        "generated_note": "Built by leads-performance-dashboard/etl/build_leads_view.py",
        "sites": sites_out,
        "default_site": CBD_SITE_CODE,
        "bands_reference": BANDS,
        "diagnostic_thresholds": DIAGNOSTIC_THRESHOLDS,
        "bands_note": "Same KPI bands apply to every site initially - split per site later if needed.",
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    build()
