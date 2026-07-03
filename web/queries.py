"""
web/queries.py
--------------
All database queries for the Tshepong Stoping Analysis web dashboard.
Uses three views in STPTM4000:
  - GANGPRODUCTIONDETAIL  : one row per (gang, workplace, period) — SQM + bonus components
  - PARTICIPANTSDETAIL    : one row per (employee, period) — individual bonus payments
  - PRODUCTIONWPDETAIL    : one row per (workplace, period) — workplace SQM pre/post adjustment

Key column notes:
  - Bonus columns in GANGPRODUCTIONDETAIL are PER-PERSON amounts.
    Multiply by GANGLABOUR to obtain total gang payout.
  - GANGPRODUCTIONBONUS = GANGFINALBREAKBONUS + GANGFINALSAFETYBONUS (production subtotal).
  - GANGTOTALSQMADJUSTED repeats across workplace rows for the same gang/period.
    Use MAX() when grouping by gang.
  - WORKPLACETOTALSQM = WPMAXLEDGESQM + WPMAXSTOPESQM per workplace — use SUM() across
    workplaces for "startup SQM" (pre-adjustment).
  - Safety indicators: GANGLTIIND, GANGDRESSINGIND, GANGFATALIND (not GANGSAFETYIND).
  - No MONTHSHIFTS column in GANGPRODUCTIONDETAIL.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
from src.database.connection import read_sql

# ── Month label helpers ────────────────────────────────────────────────────────

_MONTH_LABELS = {
    1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
    7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec",
}

def _period_label(p: int) -> str:
    y, m = p // 100, p % 100
    return f"{_MONTH_LABELS.get(m, str(m))}-{str(y)[2:]}"

def _build_period_list(period_from: int, period_to: int) -> list[int]:
    periods: list[int] = []
    y, m = period_from // 100, period_from % 100
    y_end, m_end = period_to // 100, period_to % 100
    while y * 100 + m <= y_end * 100 + m_end:
        periods.append(y * 100 + m)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return periods

# ── Filter builders ────────────────────────────────────────────────────────────

def _section_filter(section: str) -> str:
    if section and section.upper() != "ALL":
        safe = section.upper().replace("'", "''")
        return f"AND UPPER(LTRIM(RTRIM(SECTION))) = '{safe}'"
    return ""

def _gangtype_filter(gangtype: str) -> str:
    if gangtype and gangtype.upper() != "ALL":
        safe = gangtype.upper().replace("'", "''")
        return f"AND UPPER(LTRIM(RTRIM(GANGTYPE))) = '{safe}'"
    return ""

# ── PARTICIPANTSDETAIL bonus helper ───────────────────────────────────────────

def _get_participants_bonus(period_from: int, period_to: int,
                            section: str = "ALL") -> pd.DataFrame:
    """
    Total bonus per (section, period, gang) from PARTICIPANTSDETAIL.
    Returns separate STM, safety, and driller bonus columns.
    Filter: gang != 'xxx' AND crewno != '-'
    """
    sf = _section_filter(section)
    period_filter = (f"TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}"
                     if period_from > 0 and period_to > 0
                     else "TRY_CAST(PERIOD AS BIGINT) BETWEEN 200001 AND 209912")
    query = f"""
        SELECT
            LTRIM(RTRIM(SECTION))  AS section,
            LTRIM(RTRIM(PERIOD))   AS period,
            LTRIM(RTRIM(GANG))     AS gang,
            SUM(ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0)) AS total_stm_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEESAFETYBONUS    AS FLOAT), 0)) AS total_safety_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEEDRILLERBONUS   AS FLOAT), 0)) AS total_driller_bonus,
            SUM(
                ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0) +
                ISNULL(TRY_CAST(EMPLOYEESAFETYBONUS    AS FLOAT), 0) +
                ISNULL(TRY_CAST(EMPLOYEEDRILLERBONUS   AS FLOAT), 0)
            ) AS total_bonus
        FROM [PARTICIPANTSDETAIL]
        WHERE LTRIM(RTRIM(GANG))   != 'xxx'
          AND LTRIM(RTRIM(CREWNO)) != '-'
          AND {period_filter}
          {sf}
        GROUP BY
            LTRIM(RTRIM(SECTION)),
            LTRIM(RTRIM(PERIOD)),
            LTRIM(RTRIM(GANG))
    """
    df = read_sql(query)
    if not df.empty:
        df["period"] = df["period"].astype(str)
    return df

# ── Public query functions ─────────────────────────────────────────────────────

def get_available_periods() -> list[dict]:
    """All distinct periods in GANGPRODUCTIONDETAIL, newest first."""
    df = read_sql("""
        SELECT DISTINCT LTRIM(RTRIM(PERIOD)) AS period
        FROM [GANGPRODUCTIONDETAIL]
        WHERE PERIOD IS NOT NULL
          AND LTRIM(RTRIM(PERIOD)) != ''
          AND TRY_CAST(PERIOD AS BIGINT) BETWEEN 200001 AND 209912
        ORDER BY period DESC
    """)
    out = []
    for p_str in df["period"].tolist():
        try:
            p = int(str(p_str).strip())
            out.append({"value": p, "label": _period_label(p)})
        except ValueError:
            pass
    return out


def get_sections(period_from: int, period_to: int) -> list[str]:
    df = read_sql(f"""
        SELECT DISTINCT LTRIM(RTRIM(SECTION)) AS section
        FROM [GANGPRODUCTIONDETAIL]
        WHERE TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
          AND SECTION IS NOT NULL AND LTRIM(RTRIM(SECTION)) != ''
        ORDER BY section
    """)
    return df["section"].tolist()


def get_kpi_summary(period_from: int, period_to: int,
                    section: str = "ALL", gangtype: str = "ALL") -> dict:
    """Top-level KPIs: total m², total bonus (with breakdown), gang count, avg m²/gang, R/m²."""
    sf = _section_filter(section)

    # SQM from GANGPRODUCTIONDETAIL (STOPE BREAKING, deduplicated by gang)
    sqm_df = read_sql(f"""
        SELECT section, period, gang, MAX(sqm) AS sqm
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              {sf}
        ) AS q
        GROUP BY section, period, gang
    """)
    if sqm_df.empty:
        return {}
    sqm_df["period"] = sqm_df["period"].astype(str)

    bonus_df = _get_participants_bonus(period_from, period_to, section)

    total_sqm           = float(sqm_df["sqm"].sum())
    total_bonus         = float(bonus_df["total_bonus"].sum())         if not bonus_df.empty else 0.0
    total_safety_bonus  = float(bonus_df["total_safety_bonus"].sum())  if not bonus_df.empty else 0.0
    total_driller_bonus = float(bonus_df["total_driller_bonus"].sum()) if not bonus_df.empty else 0.0
    total_stm_bonus     = float(bonus_df["total_stm_bonus"].sum())     if not bonus_df.empty else 0.0
    gang_count          = int(sqm_df.groupby(["section", "gang"]).ngroups)
    avg_sqm             = float(sqm_df["sqm"].mean())
    r_per_sqm           = total_bonus / total_sqm if total_sqm > 0 else 0
    periods_seen        = sqm_df["period"].nunique()

    return {
        "total_sqm":           round(total_sqm, 0),
        "total_bonus":         round(total_bonus, 2),
        "total_safety_bonus":  round(total_safety_bonus, 2),
        "total_driller_bonus": round(total_driller_bonus, 2),
        "total_stm_bonus":     round(total_stm_bonus, 2),
        "gang_count":          gang_count,
        "avg_sqm_per_gang":    round(avg_sqm, 1),
        "r_per_sqm":           round(r_per_sqm, 2),
        "periods":             periods_seen,
    }


def get_production_trend(period_from: int, period_to: int,
                          section: str = "ALL") -> dict:
    """Monthly total adjusted m² (STOPE BREAKING) by section — for trend chart."""
    sf = _section_filter(section)
    df = read_sql(f"""
        SELECT section, period, gang, MAX(sqm) AS sqm
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              {sf}
        ) AS q
        GROUP BY section, period, gang
    """)
    if df.empty:
        return {"periods": [], "datasets": []}

    df["period"] = df["period"].astype(str)
    period_order  = sorted(df["period"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]

    pivot    = df.groupby(["section", "period"])["sqm"].sum().reset_index()
    sections = sorted(df["section"].unique())
    colors   = ["#1565C0","#2E7D32","#AD1457","#E65100","#4527A0","#00695C"]

    datasets = []
    for i, sec in enumerate(sections):
        sec_data = pivot[pivot["section"] == sec].set_index("period")
        values   = [round(float(sec_data.loc[p, "sqm"]), 0) if p in sec_data.index else 0
                    for p in period_order]
        datasets.append({
            "label":           sec,
            "data":            values,
            "borderColor":     colors[i % len(colors)],
            "backgroundColor": colors[i % len(colors)] + "33",
        })

    total_by_period = pivot.groupby("period")["sqm"].sum()
    totals = [round(float(total_by_period.get(p, 0)), 0) for p in period_order]
    datasets.append({
        "label":           "Total",
        "data":            totals,
        "borderColor":     "#FFA000",
        "backgroundColor": "#FFA00033",
        "borderWidth":     3,
        "borderDash":      [6, 3],
    })
    return {"periods": period_labels, "datasets": datasets}


def get_pre_adj_trend(period_from: int, period_to: int,
                      section: str = "ALL") -> dict:
    """Monthly Pre-Adj m² (SUM of WORKPLACETOTALSQM per gang) by section."""
    sf = _section_filter(section)
    df = read_sql(f"""
        SELECT section, period, gang, SUM(wp_sqm) AS sqm
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION))  AS section,
                LTRIM(RTRIM(PERIOD))   AS period,
                LTRIM(RTRIM(GANG))     AS gang,
                LTRIM(RTRIM(CREWNO))   AS crewno,
                TRY_CAST(WORKPLACETOTALSQM AS FLOAT) AS wp_sqm
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              {sf}
        ) AS q
        GROUP BY section, period, gang
    """)
    if df.empty:
        return {"periods": [], "datasets": []}

    df["period"] = df["period"].astype(str)
    period_order  = sorted(df["period"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]

    pivot    = df.groupby(["section", "period"])["sqm"].sum().reset_index()
    sections = sorted(df["section"].unique())
    colors   = ["#1565C0","#2E7D32","#AD1457","#E65100","#4527A0","#00695C"]

    datasets = []
    for i, sec in enumerate(sections):
        sec_data = pivot[pivot["section"] == sec].set_index("period")
        values   = [round(float(sec_data.loc[p, "sqm"]), 0) if p in sec_data.index else 0
                    for p in period_order]
        datasets.append({
            "label":           sec,
            "data":            values,
            "borderColor":     colors[i % len(colors)],
            "backgroundColor": colors[i % len(colors)] + "33",
        })

    total_by_period = pivot.groupby("period")["sqm"].sum()
    totals = [round(float(total_by_period.get(p, 0)), 0) for p in period_order]
    datasets.append({
        "label":           "Total",
        "data":            totals,
        "borderColor":     "#FFA000",
        "backgroundColor": "#FFA00033",
        "borderWidth":     3,
        "borderDash":      [6, 3],
    })
    return {"periods": period_labels, "datasets": datasets}


def get_bonus_by_gangtype(period_from: int, period_to: int,
                          section: str = "ALL") -> dict:
    """Total bonus by GANGTYPE and period — from PARTICIPANTSDETAIL with breakdown."""
    sf = _section_filter(section)
    df = read_sql(f"""
        SELECT
            LTRIM(RTRIM(GANGTYPE))  AS gangtype,
            LTRIM(RTRIM(PERIOD))    AS period,
            SUM(ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0)) AS stm_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEESAFETYBONUS    AS FLOAT), 0)) AS safety_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEEDRILLERBONUS   AS FLOAT), 0)) AS driller_bonus,
            SUM(
                ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0) +
                ISNULL(TRY_CAST(EMPLOYEESAFETYBONUS    AS FLOAT), 0) +
                ISNULL(TRY_CAST(EMPLOYEEDRILLERBONUS   AS FLOAT), 0)
            ) AS total_bonus
        FROM [PARTICIPANTSDETAIL]
        WHERE LTRIM(RTRIM(GANG))   != 'xxx'
          AND LTRIM(RTRIM(CREWNO)) != '-'
          AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
          {sf}
        GROUP BY LTRIM(RTRIM(GANGTYPE)), LTRIM(RTRIM(PERIOD))
    """)
    if df.empty:
        return {"periods": [], "rows": [], "chart": {}}

    df["period"] = df["period"].astype(str)
    period_order  = sorted(df["period"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]

    gangtypes = sorted(df["gangtype"].dropna().unique())
    gt_idx    = df.set_index(["gangtype", "period"])

    gt_colors = {
        "STOPE BREAKING":   "#1565C0",
        "STOPE CLEANING":   "#2E7D32",
        "CENTRE GULLY":     "#E65100",
        "STOPING":          "#6A1B9A",
        "SURFACE ASSISTANT":"#00695C",
        "ANCILLARY":        "#607D8B",
    }

    rows, chart_datasets = [], []
    for gt in gangtypes:
        if not gt:
            continue
        row = {"gangtype": gt}
        vals = []
        total = total_stm = total_safety = total_driller = 0.0
        for p in period_order:
            key = (gt, p)
            if key in gt_idx.index:
                stm     = float(gt_idx.loc[key, "stm_bonus"])
                safety  = float(gt_idx.loc[key, "safety_bonus"])
                driller = float(gt_idx.loc[key, "driller_bonus"])
                v       = float(gt_idx.loc[key, "total_bonus"])
            else:
                stm = safety = driller = v = 0.0
            row[p]              = round(v, 2)
            row[f"{p}_stm"]     = round(stm, 2)
            row[f"{p}_safety"]  = round(safety, 2)
            row[f"{p}_driller"] = round(driller, 2)
            vals.append(round(v, 2))
            total         += v
            total_stm     += stm
            total_safety  += safety
            total_driller += driller
        row["grand_total"]   = round(total, 2)
        row["grand_stm"]     = round(total_stm, 2)
        row["grand_safety"]  = round(total_safety, 2)
        row["grand_driller"] = round(total_driller, 2)
        if total == 0:
            continue
        rows.append(row)
        chart_datasets.append({
            "label":           gt,
            "data":            vals,
            "backgroundColor": gt_colors.get(gt.upper(), "#888"),
        })

    # Grand total row
    gt_total_row = {"gangtype": "Grand Total"}
    for p in period_order:
        pf = df[df["period"] == p]
        gt_total_row[p]              = round(float(pf["total_bonus"].sum()), 2)
        gt_total_row[f"{p}_stm"]     = round(float(pf["stm_bonus"].sum()), 2)
        gt_total_row[f"{p}_safety"]  = round(float(pf["safety_bonus"].sum()), 2)
        gt_total_row[f"{p}_driller"] = round(float(pf["driller_bonus"].sum()), 2)
    gt_total_row["grand_total"]   = round(float(df["total_bonus"].sum()), 2)
    gt_total_row["grand_stm"]     = round(float(df["stm_bonus"].sum()), 2)
    gt_total_row["grand_safety"]  = round(float(df["safety_bonus"].sum()), 2)
    gt_total_row["grand_driller"] = round(float(df["driller_bonus"].sum()), 2)
    rows.append(gt_total_row)

    return {
        "periods":     period_labels,
        "period_keys": period_order,
        "rows":        rows,
        "chart":       {"labels": period_labels, "datasets": chart_datasets},
    }


def get_bonus_by_section(period_from: int, period_to: int) -> dict:
    """Total bonus by SECTION and period — with breakdown."""
    df = _get_participants_bonus(period_from, period_to, section="ALL")
    if df.empty:
        return {"periods": [], "rows": []}

    period_order  = sorted(df["period"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]
    pivot         = df.groupby(["section", "period"])[
        ["total_bonus", "total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]
    ].sum().reset_index()
    sections = sorted(df["section"].unique())

    rows = []
    for sec in sections:
        row      = {"section": sec}
        sec_data = pivot[pivot["section"] == sec].set_index("period")
        total = total_stm = total_safety = total_driller = 0.0
        for p in period_order:
            if p in sec_data.index:
                v       = float(sec_data.loc[p, "total_bonus"])
                stm     = float(sec_data.loc[p, "total_stm_bonus"])
                safety  = float(sec_data.loc[p, "total_safety_bonus"])
                driller = float(sec_data.loc[p, "total_driller_bonus"])
            else:
                v = stm = safety = driller = 0.0
            row[p]              = round(v, 2)
            row[f"{p}_stm"]     = round(stm, 2)
            row[f"{p}_safety"]  = round(safety, 2)
            row[f"{p}_driller"] = round(driller, 2)
            total         += v
            total_stm     += stm
            total_safety  += safety
            total_driller += driller
        row["grand_total"]         = round(total, 2)
        row["grand_stm"]           = round(total_stm, 2)
        row["total_safety_bonus"]  = round(total_safety, 2)
        row["total_driller_bonus"] = round(total_driller, 2)
        rows.append(row)

    return {"periods": period_labels, "period_keys": period_order, "rows": rows}


# ── m² Range analysis ──────────────────────────────────────────────────────────

_SQM_RANGES = [
    ("Below 100",   0,    100),
    ("100 to 200",  100,  200),
    ("200 to 300",  200,  300),
    ("300 to 350",  300,  350),
    ("350 to 400",  350,  400),
    ("400 to 450",  400,  450),
    ("450 to 500",  450,  500),
    ("500 to 550",  500,  550),
    ("550 to 600",  550,  600),
    ("Greater 600", 600,  9999999),
]

def _sqm_range_label(sqm: float) -> str:
    for label, lo, hi in _SQM_RANGES:
        if lo <= sqm < hi:
            return label
    return "Greater 600"


def get_sqm_range_analysis(period_from: int, period_to: int,
                           section: str = "ALL") -> dict:
    """Gang SQM range distribution with bonus breakdown from PARTICIPANTSDETAIL."""
    sf = _section_filter(section)
    # Get gang-level SQM + labour
    df = read_sql(f"""
        SELECT section, period, crewno,
               MAX(gang)   AS gang,
               MAX(sqm)    AS sqm,
               MAX(labour) AS labour
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION))  AS section,
                LTRIM(RTRIM(PERIOD))   AS period,
                LTRIM(RTRIM(GANG))     AS gang,
                LTRIM(RTRIM(CREWNO))   AS crewno,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm,
                TRY_CAST(GANGLABOUR           AS FLOAT) AS labour
            FROM [GANGPRODUCTIONDETAIL]
            WHERE TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              AND UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              {sf}
        ) AS q
        GROUP BY section, period, crewno
    """)
    if df.empty:
        return {"ranges": [], "rows": [], "chart": {}, "period_rows": []}

    df["period"] = df["period"].astype(str)

    # Merge bonus from PARTICIPANTSDETAIL
    pbonus = _get_participants_bonus(period_from, period_to, section)
    _bonus_cols = ["total_stm_bonus", "total_safety_bonus", "total_driller_bonus", "total_bonus"]
    if not pbonus.empty:
        pbonus["period"] = pbonus["period"].astype(str)
        df = df.merge(
            pbonus[["section", "period", "gang"] + _bonus_cols],
            on=["section", "period", "gang"], how="left"
        )
        for col in _bonus_cols:
            df[col] = df[col].fillna(0)
    else:
        for col in _bonus_cols:
            df[col] = 0.0

    df["sqm_range"] = df["sqm"].apply(lambda x: _sqm_range_label(float(x or 0)))
    range_order = [r[0] for r in _SQM_RANGES]

    # Aggregate summary across all periods
    agg = df.groupby("sqm_range").agg(
        crew_count    = ("crewno",             "count"),
        avg_labour    = ("labour",             "mean"),
        avg_sqm       = ("sqm",               "mean"),
        total_stm     = ("total_stm_bonus",   "sum"),
        total_safety  = ("total_safety_bonus","sum"),
        total_driller = ("total_driller_bonus","sum"),
        total_bonus   = ("total_bonus",       "sum"),
    ).reset_index()

    rows = []
    for rng in range_order:
        r = agg[agg["sqm_range"] == rng]
        if r.empty:
            continue
        r = r.iloc[0]
        bodies    = round(float(r["avg_labour"]), 1) if float(r["avg_labour"]) > 0 else 0
        avg_sqm   = round(float(r["avg_sqm"]), 1)
        sqm_pm    = round(avg_sqm / bodies, 2) if bodies > 0 else 0
        rows.append({
            "range":         rng,
            "crew_count":    int(r["crew_count"]),
            "avg_labour":    bodies,
            "avg_sqm":       avg_sqm,
            "sqm_per_man":   sqm_pm,
            "stm_bonus":     round(float(r["total_stm"]),     2),
            "safety_bonus":  round(float(r["total_safety"]),  2),
            "driller_bonus": round(float(r["total_driller"]), 2),
            "total_bonus":   round(float(r["total_bonus"]),   2),
        })

    # Per-period distribution for trend chart
    period_pivot = df.groupby(["period", "sqm_range"])["crewno"].nunique().reset_index()
    period_pivot.columns = ["period", "sqm_range", "count"]
    period_order_list = sorted(df["period"].unique())
    period_labels     = [_period_label(int(p)) for p in period_order_list]

    range_colors = ["#ef5350","#FF9800","#FFEE58","#9CCC65","#26C6DA",
                    "#42A5F5","#5C6BC0","#AB47BC","#EC407A","#78909C"]
    chart_datasets = []
    for i, rng in enumerate(range_order):
        vals = []
        for p in period_order_list:
            sub = period_pivot[(period_pivot["period"].astype(str) == str(p)) &
                               (period_pivot["sqm_range"] == rng)]
            vals.append(int(sub["count"].sum()) if not sub.empty else 0)
        if any(v > 0 for v in vals):
            chart_datasets.append({
                "label": rng,
                "data":  vals,
                "backgroundColor": range_colors[i % len(range_colors)],
            })

    # Period bonus totals
    bonus_by_period = df.groupby("period")[
        ["total_stm_bonus", "total_safety_bonus", "total_driller_bonus", "total_bonus"]
    ].sum()

    period_rows = []
    for p in period_order_list:
        row   = {"period": _period_label(int(p)), "period_raw": str(p)}
        total = 0
        for rng in range_order:
            sub = period_pivot[(period_pivot["period"].astype(str) == str(p)) &
                               (period_pivot["sqm_range"] == rng)]
            cnt = int(sub["count"].sum()) if not sub.empty else 0
            row[rng] = cnt
            total   += cnt
        row["total"] = total
        ps = str(p)
        if ps in bonus_by_period.index:
            bp = bonus_by_period.loc[ps]
            row["stm_bonus"]     = round(float(bp["total_stm_bonus"]),    2)
            row["safety_bonus"]  = round(float(bp["total_safety_bonus"]), 2)
            row["driller_bonus"] = round(float(bp["total_driller_bonus"]),2)
            row["total_bonus"]   = round(float(bp["total_bonus"]),        2)
        else:
            for k in ["stm_bonus", "safety_bonus", "driller_bonus", "total_bonus"]:
                row[k] = 0.0
        period_rows.append(row)

    return {
        "ranges":      range_order,
        "rows":        rows,
        "chart":       {"labels": period_labels, "datasets": chart_datasets},
        "period_rows": period_rows,
    }


def get_gang_detail(period_from: int, period_to: int,
                    section: str = "ALL", gangtype: str = "ALL") -> list[dict]:
    """
    Full gang-level detail — one row per (gang, crewno, gangtype) per PERIOD.
    Tshepong-specific bonus columns: break, drill, sweep, safety (+ prod as subtotal).
    Safety indicators: LTI, Dressing, Fatal (not single GANGSAFETYIND).
    """
    sf = _section_filter(section)
    gf = _gangtype_filter(gangtype)

    df = read_sql(f"""
        SELECT
            section, period, gang, crewno, gangtype,
            MAX(adj_sqm)     AS adj_sqm,
            SUM(wp_sqm)      AS startup_sqm,
            MAX(labour)      AS labour,
            MAX(efficiency)  AS efficiency,
            MAX(break_bonus) AS break_bonus,
            MAX(drill_bonus) AS drill_bonus,
            MAX(sweep_bonus) AS sweep_bonus,
            MAX(safety_bonus)AS safety_bonus,
            MAX(lti_ind)     AS lti_ind,
            MAX(dress_ind)   AS dress_ind,
            MAX(fatal_ind)   AS fatal_ind
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION))   AS section,
                LTRIM(RTRIM(PERIOD))    AS period,
                LTRIM(RTRIM(GANG))      AS gang,
                LTRIM(RTRIM(CREWNO))    AS crewno,
                LTRIM(RTRIM(GANGTYPE))  AS gangtype,
                TRY_CAST(GANGTOTALSQMADJUSTED   AS FLOAT) AS adj_sqm,
                TRY_CAST(WORKPLACETOTALSQM      AS FLOAT) AS wp_sqm,
                TRY_CAST(GANGLABOUR             AS FLOAT) AS labour,
                TRY_CAST(GANGEFFICIENCY         AS FLOAT) AS efficiency,
                TRY_CAST(GANGFINALBREAKBONUS     AS FLOAT) AS break_bonus,
                TRY_CAST(GANGDRILLERBONUS        AS FLOAT) AS drill_bonus,
                TRY_CAST(GANGFINALSWEEPINGSBONUS AS FLOAT) AS sweep_bonus,
                TRY_CAST(GANGFINALSAFETYBONUS    AS FLOAT) AS safety_bonus,
                TRY_CAST(GANGLTIIND             AS FLOAT) AS lti_ind,
                TRY_CAST(GANGDRESSINGIND         AS FLOAT) AS dress_ind,
                TRY_CAST(GANGFATALIND            AS FLOAT) AS fatal_ind
            FROM [GANGPRODUCTIONDETAIL]
            WHERE TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              {sf} {gf}
        ) AS raw
        GROUP BY section, period, gang, crewno, gangtype
        ORDER BY section, period, gang
    """)
    if df.empty:
        return []

    df["period"] = df["period"].astype(str)

    # Multiply per-person bonus columns by labour to get gang-level totals
    for bc in ["break_bonus", "drill_bonus", "sweep_bonus", "safety_bonus"]:
        df[bc] = df[bc].fillna(0) * df["labour"].fillna(0)

    df["efficiency"] = df["efficiency"].fillna(0)
    df["sqm_range"]  = df["adj_sqm"].apply(lambda x: _sqm_range_label(float(x or 0)))

    records = []
    for _, row in df.iterrows():
        total_bonus = (float(row["break_bonus"]  or 0) +
                       float(row["drill_bonus"]   or 0) +
                       float(row["sweep_bonus"]   or 0) +
                       float(row["safety_bonus"]  or 0))
        records.append({
            "section":      row["section"],
            "period":       _period_label(int(row["period"])),
            "period_raw":   row["period"],
            "gang":         row["gang"],
            "crewno":       row["crewno"],
            "gangtype":     row["gangtype"],
            "crewstartupsqm": round(float(row["startup_sqm"] or 0), 0),
            "sqm":          round(float(row["adj_sqm"]     or 0), 0),
            "sqm_range":    row["sqm_range"],
            "labour":       round(float(row["labour"]      or 0), 2),
            "efficiency":   round(float(row["efficiency"]  or 0), 2),
            "break_bonus":  round(float(row["break_bonus"] or 0), 2),
            "drill_bonus":  round(float(row["drill_bonus"] or 0), 2),
            "sweep_bonus":  round(float(row["sweep_bonus"] or 0), 2),
            "safety_bonus": round(float(row["safety_bonus"]or 0), 2),
            "total_bonus":  round(total_bonus, 2),
            "lti_ind":      int(row["lti_ind"]   or 0),
            "dress_ind":    int(row["dress_ind"] or 0),
            "fatal_ind":    int(row["fatal_ind"] or 0),
        })
    return records


def get_rands_per_sqm(period_from: int, period_to: int,
                      section: str = "ALL") -> dict:
    """R/m² trend — STOPE BREAKING only with bonus breakdown."""
    sf = _section_filter(section)

    raw_df = read_sql(f"""
        SELECT period, gang,
               MAX(adj_sqm) AS adj_sqm,
               SUM(wp_sqm)  AS startup_sqm
        FROM (
            SELECT
                LTRIM(RTRIM(PERIOD)) AS period,
                LTRIM(RTRIM(GANG))   AS gang,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS adj_sqm,
                TRY_CAST(WORKPLACETOTALSQM    AS FLOAT) AS wp_sqm
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              {sf}
        ) AS q
        GROUP BY period, gang
    """)
    if raw_df.empty:
        return {"labels": [], "r_per_sqm": [], "startup_r_per_sqm": [],
                "total_bonus": [], "total_sqm": [], "startup_sqm": [],
                "safety_bonus": [], "driller_bonus": [], "stm_bonus": []}

    raw_df["period"] = raw_df["period"].astype(str)
    sqm_agg = raw_df.groupby("period").agg(
        sqm     =("adj_sqm",    "sum"),
        startup =("startup_sqm","sum"),
    ).reset_index()

    bonus_df  = _get_participants_bonus(period_from, period_to, section)
    if not bonus_df.empty:
        bonus_agg = bonus_df.groupby("period")[
            ["total_bonus", "total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]
        ].sum().reset_index()
    else:
        bonus_agg = pd.DataFrame(columns=["period", "total_bonus", "total_stm_bonus",
                                           "total_safety_bonus", "total_driller_bonus"])

    period_order = sorted(raw_df["period"].unique())
    labels, r_sqm, startup_r_sqm, bonuses, sqms, startup_sqms = [], [], [], [], [], []
    safety_bonuses, driller_bonuses, stm_bonuses = [], [], []

    for p in period_order:
        sqm_row   = sqm_agg[sqm_agg["period"] == p]
        bonus_row = bonus_agg[bonus_agg["period"] == p] if not bonus_agg.empty else pd.DataFrame()

        sqm     = float(sqm_row["sqm"].iloc[0])     if not sqm_row.empty     else 0
        startup = float(sqm_row["startup"].iloc[0]) if not sqm_row.empty     else 0
        bonus   = float(bonus_row["total_bonus"].iloc[0])        if not bonus_row.empty else 0
        safety  = float(bonus_row["total_safety_bonus"].iloc[0]) if not bonus_row.empty else 0
        driller = float(bonus_row["total_driller_bonus"].iloc[0])if not bonus_row.empty else 0
        stm     = float(bonus_row["total_stm_bonus"].iloc[0])    if not bonus_row.empty else 0

        labels.append(_period_label(int(p)))
        r_sqm.append(round(bonus / sqm,     2) if sqm     > 0 else 0)
        startup_r_sqm.append(round(bonus / startup, 2) if startup > 0 else 0)
        bonuses.append(round(bonus,   2))
        sqms.append(round(sqm,        0))
        startup_sqms.append(round(startup, 0))
        safety_bonuses.append(round(safety, 2))
        driller_bonuses.append(round(driller, 2))
        stm_bonuses.append(round(stm, 2))

    return {
        "labels":            labels,
        "r_per_sqm":         r_sqm,
        "startup_r_per_sqm": startup_r_sqm,
        "total_bonus":       bonuses,
        "total_sqm":         sqms,
        "startup_sqm":       startup_sqms,
        "safety_bonus":      safety_bonuses,
        "driller_bonus":     driller_bonuses,
        "stm_bonus":         stm_bonuses,
    }


def get_simulation_data(period_from: int, period_to: int,
                        section: str = "ALL") -> dict:
    """Per-gang STOPE BREAKING data for the quota simulation tab."""
    sf = _section_filter(section)
    sqm_df = read_sql(f"""
        SELECT section, period, gang, crewno,
               MAX(sqm) AS sqm, MAX(labour) AS labour
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                LTRIM(RTRIM(CREWNO))  AS crewno,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm,
                TRY_CAST(GANGLABOUR           AS FLOAT) AS labour
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              {sf}
        ) AS q
        GROUP BY section, period, gang, crewno
    """)
    if sqm_df.empty:
        return {"gangs": [], "periods": [], "sections": [],
                "section_stm_rates": {}, "overall_stm_rate": 0.0,
                "total_actual_sqm": 0.0, "total_actual_bonus": 0.0}

    sqm_df["period"] = sqm_df["period"].astype(str)
    sqm_df["sqm"]    = sqm_df["sqm"].fillna(0).astype(float)
    sqm_df["labour"] = sqm_df["labour"].fillna(0).astype(float)

    bonus_df = _get_participants_bonus(period_from, period_to, section)
    bonus_cols = ["section", "period", "gang",
                  "total_bonus", "total_stm_bonus",
                  "total_safety_bonus", "total_driller_bonus"]
    if not bonus_df.empty:
        sqm_df = sqm_df.merge(bonus_df[bonus_cols],
                              on=["section", "period", "gang"], how="left")
    for col in ["total_bonus", "total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]:
        if col not in sqm_df.columns:
            sqm_df[col] = 0.0
        else:
            sqm_df[col] = sqm_df[col].fillna(0).astype(float)

    sqm_df["stm_r_per_sqm"] = sqm_df.apply(
        lambda r: r["total_stm_bonus"] / r["sqm"] if r["sqm"] > 0 else 0.0, axis=1
    )

    period_order = sorted(sqm_df["period"].unique())
    sections     = sorted(sqm_df["section"].unique())

    sec_agg = sqm_df.groupby("section")[["sqm", "total_stm_bonus"]].sum()
    section_stm_rates: dict = {}
    for sec in sections:
        if sec in sec_agg.index:
            s = float(sec_agg.loc[sec, "sqm"])
            b = float(sec_agg.loc[sec, "total_stm_bonus"])
            section_stm_rates[sec] = round(b / s, 4) if s > 0 else 0.0

    total_sqm_all       = float(sqm_df["sqm"].sum())
    total_bonus_all     = float(sqm_df["total_bonus"].sum())
    total_stm_bonus_all = float(sqm_df["total_stm_bonus"].sum())
    overall_stm_rate    = round(total_stm_bonus_all / total_sqm_all, 4) if total_sqm_all > 0 else 0.0

    gangs = []
    for _, row in sqm_df.iterrows():
        gangs.append({
            "section":       row["section"],
            "period":        _period_label(int(row["period"])),
            "period_raw":    row["period"],
            "gang":          row["gang"],
            "crewno":        row["crewno"],
            "actual_sqm":    round(float(row["sqm"]), 0),
            "labour":        round(float(row["labour"]), 1),
            "actual_bonus":  round(float(row["total_bonus"]), 2),
            "stm_bonus":     round(float(row["total_stm_bonus"]), 2),
            "safety_bonus":  round(float(row["total_safety_bonus"]), 2),
            "driller_bonus": round(float(row["total_driller_bonus"]), 2),
            "stm_r_per_sqm": round(float(row["stm_r_per_sqm"]), 4),
        })

    return {
        "gangs":              gangs,
        "periods":            [_period_label(int(p)) for p in period_order],
        "period_keys":        period_order,
        "sections":           sections,
        "section_stm_rates":  section_stm_rates,
        "overall_stm_rate":   overall_stm_rate,
        "total_actual_sqm":   round(total_sqm_all, 0),
        "total_actual_bonus": round(total_bonus_all, 2),
    }


def get_forecast_data(period_from: int, period_to: int,
                      section: str = "ALL") -> dict:
    """All-history STOPE BREAKING aggregates for regression/forecast. Ignores period_from/to."""
    sf = _section_filter(section)
    sqm_df = read_sql(f"""
        SELECT section, period, gang, crewno,
               MAX(sqm) AS sqm, MAX(labour) AS labour
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                LTRIM(RTRIM(CREWNO))  AS crewno,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm,
                TRY_CAST(GANGLABOUR           AS FLOAT) AS labour
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN 200001 AND 209912
              {sf}
        ) AS q
        GROUP BY section, period, gang, crewno
    """)
    _empty = {"period_keys": [], "period_labels": [], "total_sqm": [],
              "total_bonus": [], "gang_count": [], "r_per_sqm": [],
              "sections": [], "section_sqm": {}, "section_bonus": {},
              "gang_slopes": [], "n_periods": 0,
              "total_labour": [], "stm_bonus": [], "avg_r_man": []}
    if sqm_df.empty:
        return _empty

    sqm_df["period"] = sqm_df["period"].astype(str)
    sqm_df["sqm"]    = sqm_df["sqm"].fillna(0).astype(float)
    sqm_df["labour"] = sqm_df["labour"].fillna(0).astype(float)

    period_order  = sorted(sqm_df["period"].unique())
    sections      = sorted(sqm_df["section"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]

    # All bonus history from PARTICIPANTSDETAIL (views include all history)
    bonus_df = _get_participants_bonus(0, 0, section)  # 0,0 → all periods
    if not bonus_df.empty:
        bonus_df["period"] = bonus_df["period"].astype(str)
        sb_keys  = sqm_df[["section", "period", "gang"]].drop_duplicates()
        bonus_sb = sb_keys.merge(
            bonus_df[["section", "period", "gang", "total_bonus", "total_stm_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        bonus_sb["total_bonus"]     = bonus_sb["total_bonus"].fillna(0).astype(float)
        bonus_sb["total_stm_bonus"] = bonus_sb["total_stm_bonus"].fillna(0).astype(float)
        per_period_bonus = bonus_sb.groupby("period")[["total_bonus", "total_stm_bonus"]].sum().reset_index()
        sec_bonus_long   = bonus_sb.groupby(["section", "period"])["total_bonus"].sum().reset_index()
    else:
        per_period_bonus = pd.DataFrame(columns=["period", "total_bonus", "total_stm_bonus"])
        sec_bonus_long   = pd.DataFrame(columns=["section", "period", "total_bonus"])

    psqm = sqm_df.groupby("period").agg(
        total_sqm   =("sqm",    "sum"),
        gang_count  =("crewno", "nunique"),
        total_labour=("labour", "sum"),
    ).reset_index()
    psqm_idx = psqm.set_index("period")
    pbon_idx = per_period_bonus.set_index("period") if not per_period_bonus.empty else pd.DataFrame()

    total_sqm_list, total_bonus_list, gang_count_list, r_per_sqm_list = [], [], [], []
    total_labour_list, stm_bonus_list, avg_r_man_list = [], [], []
    for p in period_order:
        tsqm  = float(psqm_idx.loc[p, "total_sqm"])    if p in psqm_idx.index else 0.0
        gc    = int(psqm_idx.loc[p, "gang_count"])      if p in psqm_idx.index else 0
        tlab  = float(psqm_idx.loc[p, "total_labour"])  if p in psqm_idx.index else 0.0
        tb    = float(pbon_idx.loc[p, "total_bonus"])    if (not pbon_idx.empty and p in pbon_idx.index) else 0.0
        tstm  = float(pbon_idx.loc[p, "total_stm_bonus"]) if (not pbon_idx.empty and p in pbon_idx.index) else 0.0
        total_sqm_list.append(round(tsqm, 0))
        total_bonus_list.append(round(tb, 2))
        gang_count_list.append(gc)
        r_per_sqm_list.append(round(tb / tsqm, 4) if tsqm > 0 else 0.0)
        total_labour_list.append(round(tlab, 1))
        stm_bonus_list.append(round(tstm, 2))
        avg_r_man_list.append(round(tstm / tlab, 2) if tlab > 0 else 0.0)

    sec_sqm_piv = sqm_df.groupby(["section", "period"])["sqm"].sum().reset_index()
    section_sqm_dict: dict = {}
    section_bonus_dict: dict = {}
    sec_bon_idx_map: dict = {}
    if not sec_bonus_long.empty:
        for sec in sections:
            sb = sec_bonus_long[sec_bonus_long["section"] == sec].set_index("period")
            sec_bon_idx_map[sec] = sb
    for sec in sections:
        sq = sec_sqm_piv[sec_sqm_piv["section"] == sec].set_index("period")
        sb = sec_bon_idx_map.get(sec, pd.DataFrame())
        section_sqm_dict[sec]   = [round(float(sq.loc[p, "sqm"]), 0) if p in sq.index else 0.0
                                    for p in period_order]
        section_bonus_dict[sec] = [round(float(sb.loc[p, "total_bonus"]), 2)
                                    if (not sb.empty and p in sb.index) else 0.0
                                    for p in period_order]

    # Per-gang linear slopes (last 12 months)
    last_12_periods = set(period_order[-12:])
    gang_period_df  = sqm_df.groupby(["section", "gang", "period"])["sqm"].sum().reset_index()
    gang_slopes = []
    for (sec, gang), gdf in gang_period_df.groupby(["section", "gang"]):
        gdf = gdf.sort_values("period")
        gdf_recent = gdf[gdf["period"].isin(last_12_periods)]
        if len(gdf_recent) < 3:
            continue
        ys    = gdf_recent["sqm"].tolist()
        n_per = len(ys)
        xm    = (n_per - 1) / 2
        ym    = sum(ys) / n_per
        sxy   = sum((i - xm) * (ys[i] - ym) for i in range(n_per))
        sxx   = sum((i - xm) ** 2 for i in range(n_per))
        slope = sxy / sxx if sxx > 0 else 0.0
        last3_avg  = sum(ys[-3:]) / 3
        prior3     = ys[-6:-3]
        pct_vs_prior3 = (round((last3_avg - sum(prior3)/len(prior3)) / (sum(prior3)/len(prior3)) * 100, 1)
                         if prior3 and sum(prior3)/len(prior3) > 0 else None)
        gang_slopes.append({
            "section":       sec,
            "gang":          gang,
            "slope":         round(slope, 2),
            "last_sqm":      round(float(gdf_recent["sqm"].iloc[-1]), 0),
            "last_period":   _period_label(int(gdf_recent["period"].iloc[-1])),
            "n_periods":     n_per,
            "pct_vs_prior3": pct_vs_prior3,
        })
    gang_slopes.sort(key=lambda x: x["slope"])

    return {
        "period_keys":   period_order,
        "period_labels": period_labels,
        "total_sqm":     total_sqm_list,
        "total_bonus":   total_bonus_list,
        "gang_count":    gang_count_list,
        "r_per_sqm":     r_per_sqm_list,
        "sections":      sections,
        "section_sqm":   section_sqm_dict,
        "section_bonus": section_bonus_dict,
        "gang_slopes":   gang_slopes,
        "n_periods":     len(period_order),
        "total_labour":  total_labour_list,
        "stm_bonus":     stm_bonus_list,
        "avg_r_man":     avg_r_man_list,
    }


def get_period_performance(period_from: int, period_to: int,
                           section: str = "ALL") -> dict:
    """Period-by-period aggregates for the Forecast tab (respects period range)."""
    sf = _section_filter(section)
    _empty = {"period_keys": [], "period_labels": [], "total_sqm": [],
              "gang_count": [], "total_labour": [], "meas_shifts": [],
              "stm_bonus": [], "total_bonus": [], "avg_r_man": []}

    # Gang-level: sqm + labour
    sqm_df = read_sql(f"""
        SELECT section, period, gang, crewno,
               MAX(labour) AS labour
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                LTRIM(RTRIM(CREWNO))  AS crewno,
                TRY_CAST(GANGLABOUR AS FLOAT) AS labour
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              {sf}
        ) AS q
        GROUP BY section, period, gang, crewno
    """)
    if sqm_df.empty:
        return _empty

    sqm_df["period"] = sqm_df["period"].astype(str)
    sqm_df["labour"] = sqm_df["labour"].fillna(0).astype(float)

    period_order  = sorted(sqm_df["period"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]

    # Pre-Adj SQM: SUM(WORKPLACETOTALSQM) per gang, then sum across gangs per period
    wp_df = read_sql(f"""
        SELECT period, SUM(startup_sqm) AS startup_sqm
        FROM (
            SELECT
                LTRIM(RTRIM(PERIOD)) AS period,
                LTRIM(RTRIM(GANG))   AS gang,
                SUM(TRY_CAST(WORKPLACETOTALSQM AS FLOAT)) AS startup_sqm
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              {sf}
            GROUP BY LTRIM(RTRIM(PERIOD)), LTRIM(RTRIM(GANG))
        ) AS q
        GROUP BY period
    """)
    wp_df["period"] = wp_df["period"].astype(str)
    wp_idx = wp_df.set_index("period") if not wp_df.empty else pd.DataFrame()

    # Bonus from PARTICIPANTSDETAIL
    bonus_df = _get_participants_bonus(period_from, period_to, section)
    if not bonus_df.empty:
        bonus_df["period"] = bonus_df["period"].astype(str)
        sb_keys  = sqm_df[["section", "period", "gang"]].drop_duplicates()
        bonus_sb = sb_keys.merge(
            bonus_df[["section", "period", "gang", "total_stm_bonus", "total_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        for col in ["total_stm_bonus", "total_bonus"]:
            bonus_sb[col] = bonus_sb[col].fillna(0).astype(float)
        per_period = bonus_sb.groupby("period")[["total_stm_bonus", "total_bonus"]].sum().reset_index()
        pbon_idx = per_period.set_index("period")
    else:
        pbon_idx = pd.DataFrame()

    psqm = sqm_df.groupby("period").agg(
        gang_count  =("crewno",  "nunique"),
        total_labour=("labour",  "sum"),
    ).reset_index().set_index("period")

    total_sqm_list, gang_count_list, total_labour_list = [], [], []
    stm_bonus_list, total_bonus_list, avg_r_man_list, meas_shifts_list = [], [], [], []
    for p in period_order:
        tsqm  = float(wp_idx.loc[p, "startup_sqm"]) if (not wp_idx.empty and p in wp_idx.index) else 0.0
        gc    = int(psqm.loc[p, "gang_count"])       if p in psqm.index else 0
        tlab  = float(psqm.loc[p, "total_labour"])   if p in psqm.index else 0.0
        tstm  = float(pbon_idx.loc[p, "total_stm_bonus"]) if (not pbon_idx.empty and p in pbon_idx.index) else 0.0
        tbon  = float(pbon_idx.loc[p, "total_bonus"])      if (not pbon_idx.empty and p in pbon_idx.index) else 0.0
        total_sqm_list.append(round(tsqm, 0))
        gang_count_list.append(gc)
        total_labour_list.append(round(tlab, 1))
        meas_shifts_list.append(None)   # no MONTHSHIFTS in GANGPRODUCTIONDETAIL
        stm_bonus_list.append(round(tstm, 2))
        total_bonus_list.append(round(tbon, 2))
        avg_r_man_list.append(round(tstm / tlab, 2) if tlab > 0 else 0.0)

    return {
        "period_keys":   period_order,
        "period_labels": period_labels,
        "total_sqm":     total_sqm_list,
        "gang_count":    gang_count_list,
        "total_labour":  total_labour_list,
        "meas_shifts":   meas_shifts_list,
        "stm_bonus":     stm_bonus_list,
        "total_bonus":   total_bonus_list,
        "avg_r_man":     avg_r_man_list,
    }


def get_gang_production_detail(gang: str, period_from: int, period_to: int) -> list[dict]:
    """Workplace-level production detail for a STOPE BREAKING gang.

    Joins GANGPRODUCTIONDETAIL (workplace identifiers per gang) with
    PRODUCTIONWPDETAIL (SQM detail per workplace).
    PRODUCTIONWPDETAIL columns used:
      WPMAXLEDGESQM  → startup_ledgesqm
      WPMAXSTOPESQM  → startup_stopesqm
      WPPRETOTALM2   → startup_totalsqm
      WPTOTALM2      → totalm2 (adjusted)
      DIP_FACTOR     → wideraisefactor
      WPTOTALM2 - WPPRETOTALM2 → extram2 / m2_variance
    """
    if not gang:
        return []
    gang_clean = gang.strip().replace("'", "''")

    df = read_sql(f"""
        SELECT
            LTRIM(RTRIM(g.SECTION))   AS section,
            LTRIM(RTRIM(g.PERIOD))    AS period,
            LTRIM(RTRIM(g.GANG))      AS gang,
            LTRIM(RTRIM(g.WORKPLACE)) AS workplace,
            MAX(ISNULL(TRY_CAST(p.WPMAXLEDGESQM AS FLOAT), 0)) AS startup_ledgesqm,
            MAX(ISNULL(TRY_CAST(p.WPMAXSTOPESQM AS FLOAT), 0)) AS startup_stopesqm,
            MAX(ISNULL(TRY_CAST(p.WPPRETOTALM2  AS FLOAT), 0)) AS startup_totalsqm,
            MAX(ISNULL(TRY_CAST(p.WPTOTALM2     AS FLOAT), 0)
              - ISNULL(TRY_CAST(p.WPPRETOTALM2  AS FLOAT), 0))  AS extram2,
            MAX(ISNULL(TRY_CAST(p.DIP_FACTOR    AS FLOAT), 0))  AS wideraisefactor,
            MAX(ISNULL(TRY_CAST(p.WPTOTALM2     AS FLOAT), 0))  AS totalm2,
            MAX(ISNULL(TRY_CAST(p.WPTOTALM2     AS FLOAT), 0)
              - ISNULL(TRY_CAST(p.WPPRETOTALM2  AS FLOAT), 0))  AS m2_variance
        FROM [GANGPRODUCTIONDETAIL] g
        LEFT JOIN [PRODUCTIONWPDETAIL] p
            ON LTRIM(RTRIM(g.SECTION))   = LTRIM(RTRIM(p.SECTION))
           AND LTRIM(RTRIM(g.PERIOD))    = LTRIM(RTRIM(p.PERIOD))
           AND LTRIM(RTRIM(g.WORKPLACE)) = LTRIM(RTRIM(p.WORKPLACE))
        WHERE LTRIM(RTRIM(g.GANG)) = '{gang_clean}'
          AND UPPER(LTRIM(RTRIM(g.GANGTYPE))) = 'STOPE BREAKING'
          AND LEN(LTRIM(RTRIM(g.CREWNO))) >= 8
          AND TRY_CAST(g.PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
        GROUP BY
            LTRIM(RTRIM(g.SECTION)),
            LTRIM(RTRIM(g.PERIOD)),
            LTRIM(RTRIM(g.GANG)),
            LTRIM(RTRIM(g.WORKPLACE))
        ORDER BY period, section, workplace
    """)
    if df.empty:
        return []

    records = []
    for _, row in df.iterrows():
        records.append({
            "section":          str(row["section"] or ""),
            "period":           _period_label(int(row["period"])),
            "gang":             str(row["gang"] or ""),
            "workplace":        str(row["workplace"] or ""),
            "startup_ledgesqm": round(float(row["startup_ledgesqm"] or 0), 2),
            "startup_stopesqm": round(float(row["startup_stopesqm"] or 0), 2),
            "startup_totalsqm": round(float(row["startup_totalsqm"] or 0), 2),
            "extram2":          round(float(row["extram2"]           or 0), 2),
            "wideraisefactor":  round(float(row["wideraisefactor"]   or 0), 4),
            "totalm2":          round(float(row["totalm2"]           or 0), 2),
            "m2_variance":      round(float(row["m2_variance"]       or 0), 2),
        })
    return records
