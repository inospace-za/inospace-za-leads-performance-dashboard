"""
Transform _incoming/data.json + _incoming/history.json (fetched from the source repo)
into site/leads_data.json: a CBD-only, leads-performance-focused view scored against
agreed KPI bands.

Schema confirmed against a real data.json/history.json sample (2026-08-14):
  data["by_site"][site_id]["leads"]        -> placed_total, converted_to_lease, conversion_pct (0-100)
  data["by_site"][site_id]["moveins"/"moveouts"/"net_moves"]
  data["by_site"][site_id]["occ_pct_units"/"occ_pct_area"]  -> percentages 0-100, NOT fractions
  data["sites"]                             -> list of {id, name, code, city} (metadata only)
  history.json                              -> top-level list of daily snapshots:
    {date, portfolio_occ_pct, portfolio_units_occupied, by_site_occ_pct: {site_id: pct},
     leads_placed, leases_signed, net_moves}
  Per-site history is ONLY reliable for occupancy % (by_site_occ_pct, unit-based).
  leads_placed/leases_signed/net_moves in history.json are portfolio-blended, not
  split by site - so per-site historical leads/move-ins are not available yet.

Usage:
    python etl/build_leads_view.py
"""
import json
import os

INCOMING_DIR = "_incoming"
OUT_PATH = "docs/leads_data.json"

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

def load_incoming():
    with open(os.path.join(INCOMING_DIR, "data.json")) as f:
        data = json.load(f)
    with open(os.path.join(INCOMING_DIR, "history.json")) as f:
        history = json.load(f)
    return data, history

def extract_site_current_month(data, site_code):
    """
    Real shape (confirmed 2026-08-14 sample):
        data["by_site"][site_code]["leads"]["placed_total"]
        data["by_site"][site_code]["leads"]["converted_to_lease"]
        data["by_site"][site_code]["leads"]["conversion_pct"]   # 0-100
        data["by_site"][site_code]["moveins"]
        data["by_site"][site_code]["moveouts"]
        data["by_site"][site_code]["net_moves"]
        data["by_site"][site_code]["occ_pct_units"]             # 0-100
        data["by_site"][site_code]["occ_pct_area"]               # 0-100
    """
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

def extract_site_monthly_history(history, site_code):
    """
    history.json is a top-level list of daily snapshots:
        {date, portfolio_occ_pct, portfolio_units_occupied,
         by_site_occ_pct: {site_id: pct}, leads_placed, leases_signed, net_moves}

    Confirmed limitation: leads_placed / leases_signed / net_moves are
    portfolio-blended (not split by site). Only by_site_occ_pct is reliably
    per-site, and it matches occ_pct_units (unit-based occupancy), not area.

    Buckets by month, keeping the last snapshot seen per month (oldest first).
    genuine_enquiries / conversions / move_ins / move_outs / occupancy_sqm_pct
    stay None until the source pipeline exposes per-site leads history.
    """
    months = {}
    for day in history:
        date = day.get("date")
        if not date:
            continue
        month = date[:7]  # YYYY-MM
        occ_pct_units = (day.get("by_site_occ_pct") or {}).get(site_code)
        months[month] = {
            "month": month,
            "genuine_enquiries": None,
            "conversions": None,
            "move_ins": None,
            "move_outs": None,
            "occupancy_sqm_pct": None,
            "occupancy_units_pct": (occ_pct_units / 100) if occ_pct_units is not None else None,
        }
    return [months[m] for m in sorted(months.keys())]

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
