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
    NOTE: GANGFINALBREAKBONUS is an aliased name (kept for compatibility with the shared
    GANGPRODUCTIONDETAIL schema used across Harmony sites). At Tshepong it is NOT a flat
    rate-per-m² breaking bonus — the source column is GANGFINALEFFICIENCYBONUS, driven by
    GANGEFFICIENCY (m²/empl). Per the "Thibakotsi Stoping Incentive Scheme Cat 4-8" policy
    (JB_202603_STPTEAM_REV04), teams below the
    entry-level efficiency (Wide Raise panels excluded) earn zero on this component; above
    it, the amount is looked up from an m²-by-team-size table and then adjusted by netting
    (WORKPLACENETTINGRATE) and the B-Reef stope-width factor (B_REEF_SW_FACTOR). We label it
    "Efficiency Bonus" in this app's output to match how it's actually calculated.
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


def _prior_period_range(period_from: int, period_to: int) -> tuple[int, int]:
    """Return the period range of the same length immediately before (period_from, period_to)."""
    n = len(_build_period_list(period_from, period_to))
    # prior_to = one month before period_from
    y, m = period_from // 100, period_from % 100
    m -= 1
    if m == 0:
        m, y = 12, y - 1
    prior_to = y * 100 + m
    # prior_from = n-1 months before prior_to
    m -= (n - 1)
    while m <= 0:
        m += 12
        y -= 1
    return y * 100 + m, prior_to

# ── Filter builders ────────────────────────────────────────────────────────────

def _section_filter(section: str) -> str:
    if section and section.upper() != "ALL":
        safe = section.upper().replace("'", "''")
        return f"AND UPPER(LTRIM(RTRIM(SECTION))) = '{safe}'"
    return ""

def _gangtype_filter(gangtype: str) -> str:
    """gangtype is "ALL", a single type, or a comma-separated list of types (the filter
    bar's Gang Types control is a multi-select) — emits = 'X' for one, IN (...) for several.
    """
    if not gangtype or gangtype.upper() == "ALL":
        return ""
    types = [t.strip().upper().replace("'", "''") for t in gangtype.split(",") if t.strip()]
    if not types:
        return ""
    if len(types) == 1:
        return f"AND UPPER(LTRIM(RTRIM(GANGTYPE))) = '{types[0]}'"
    in_list = ", ".join(f"'{t}'" for t in types)
    return f"AND UPPER(LTRIM(RTRIM(GANGTYPE))) IN ({in_list})"

# ── PARTICIPANTSDETAIL bonus helper ───────────────────────────────────────────

# PARTICIPANTSDETAIL's own view definition LEFT JOINs GANGLINKEARN on (SECTION, PERIOD, GANG)
# only, with no WORKPLACE in the join condition — but GANGLINKEARN has one row per (gang,
# workplace, period), so any gang that worked more than one workplace in a period fans every
# one of its employees' rows out once per extra workplace. Confirmed on real data: period
# 202606 had 1,503 raw rows in the view vs 1,333 truly distinct ones, inflating that period's
# STM+Safety+Driller total by ~16.5% (R3,931,563.43 shown vs R3,374,177.18 correct). This is a
# bug in the shared database view itself (same flaw found independently in a sibling site's
# database), not something introduced by this app — but since the view can't safely be
# rewritten from here (shared object, other consumers may depend on it), every query that
# reads PARTICIPANTSDETAIL directly must dedupe first.
#
# The duplicate rows are byte-for-byte identical except for CREWNO (the one column the join
# actually contributes) — a narrower dedup key like EMPLOYEE_NO alone is NOT safe: 66
# employees in that same period genuinely have more than one row because they transferred
# gangs mid-period (policy §10.5), and a coarser key would silently merge those real,
# distinct payments. (SECTION, PERIOD, EMPLOYEE_NO, GANG, WAGECODE) is confirmed to reproduce
# the true row count exactly (1,333) against the same period — a plain SELECT DISTINCT across
# all columns gives the same correct count but is far more expensive (a wide multi-column
# distinct over the full multi-period UNION ALL the view is built from — timed out at 120s+
# on real queries); GROUP BY on this narrower key with MAX() on the remaining columns is the
# same dedup, cheaper, because MAX() is safe here — every column being maxed comes from
# PARTICIPANTSEARN (the non-duplicated side of the join), so it's identical across all
# fanned-out copies of a given real row; only CREWNO (from the join) can rarely differ, and
# it isn't used in any bonus calculation, only as a filter elsewhere.
#
# Takes the caller's period filter and applies it BEFORE the GROUP BY, not after — the view
# is a 12-months-and-growing UNION ALL, so deduping the whole thing on every call (then
# filtering the result) meant even a single-period query cost as much as an all-history one.
# Pushing the filter in first cut a full endpoint sweep from 120s+ back close to baseline.
def _participants_dedup_sql(period_filter: str) -> str:
    return f"""(
        SELECT
            SECTION, PERIOD, EMPLOYEE_NO, GANG, WAGECODE,
            MAX(BUSSUNIT) AS BUSSUNIT, MAX(CREWNO) AS CREWNO, MAX(GANGTYPE) AS GANGTYPE,
            MAX(WAGE_DESCRIPTION) AS WAGE_DESCRIPTION,
            MAX(EMPLOYEESUPERVISIONRATE) AS EMPLOYEESUPERVISIONRATE,
            MAX(EMPLOYEESTOPETEAMBONUS) AS EMPLOYEESTOPETEAMBONUS,
            MAX(EMPLOYEESAFETYBONUS) AS EMPLOYEESAFETYBONUS,
            MAX(EMPLOYEEDRILLERBONUS) AS EMPLOYEEDRILLERBONUS,
            MAX(EMPLOYEEAWOPPENALTY) AS EMPLOYEEAWOPPENALTY
        FROM [PARTICIPANTSDETAIL]
        WHERE {period_filter}
        GROUP BY SECTION, PERIOD, EMPLOYEE_NO, GANG, WAGECODE
    )"""


def _get_participants_bonus(period_from: int, period_to: int,
                            section: str = "ALL") -> pd.DataFrame:
    """
    Total bonus per (section, period, gang) from PARTICIPANTSDETAIL.
    Returns separate STM, safety, and driller bonus columns.
    Filter: gang != 'xxx' AND crewno != '-'

    total_bonus = SUM(EMPLOYEESTOPETEAMBONUS) ONLY. Verified per-employee against the source
    system's own EMPLOYEETOTALBONUS column (not exposed by this view), across three periods
    spanning Dec 2025 - Jun 2026 (202512, 202601, 202606): dedupe raw PARTICIPANTSEARN{period}
    to one row per EMPLOYEE_NO (MAX(EMPLOYEETOTALBONUS) — confirmed constant across all of an
    employee's rows in a period, i.e. it's a period-level broadcast, not a per-row figure) and
    SUM — that matches SUM(EMPLOYEESTOPETEAMBONUS) across every raw row with NO dedup needed
    (202606: R2,659,911.34 = R2,659,911.34; 202512: R5,103,029.14 = R5,103,029.14) to the cent.
    EMPLOYEESAFETYBONUS is consistently exactly 20% of EMPLOYEESTOPETEAMBONUS (it's a reported
    breakdown already inside STM, not an addition), and EMPLOYEEDRILLERBONUS is a reference
    rate that does not add to the employee's real payout — even for employees whose
    WAGE_DESCRIPTION is a driller role, EMPLOYEETOTALBONUS still equals just their STM figure.
    Adding safety/driller on top (the old formula) double/triple-counted and was the root
    cause of the 1.20x-2.23x overstatement found when cross-checking against EMPLOYEETOTALBONUS.
    total_safety_bonus/total_driller_bonus are kept as separate informational fields only —
    never sum them into total_bonus.

    KNOWN RESIDUAL (documented, not silently absorbed): 202601 had one row with a nonzero
    EMPLOYEEPENALTY (-694.84, i.e. a credit) that EMPLOYEETOTALBONUS nets against STM but
    EMPLOYEESTOPETEAMBONUS alone does not — EMPLOYEEPENALTY is NOT exposed by PARTICIPANTSDETAIL
    (confirmed via INFORMATION_SCHEMA.COLUMNS: only EMPLOYEEAWOPPENALTY is), so netting it would
    require bypassing the view and reading raw PARTICIPANTSEARN{period} tables directly —
    a real architecture change to recover a column that was nonzero in only 1 of 1,113 employees
    in the one period it appeared (0 in the other two periods checked), worth ~0.05% of that
    period's total. A separate, unrelated one-off was also found in 202601 (employee Z9150639:
    two same-period rows on different gangs, with the real payout sitting on a zero-STM row) —
    a source-data anomaly, not a formula gap; ~R707 on ~R1.35M for that period.
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
            SUM(ISNULL(TRY_CAST(EMPLOYEEAWOPPENALTY    AS FLOAT), 0)) AS total_awop_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0)) AS total_bonus
        FROM {_participants_dedup_sql(period_filter)} AS pd
        WHERE LTRIM(RTRIM(GANG))   != 'xxx'
          AND LTRIM(RTRIM(CREWNO)) != '-'
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

    # SQM from GANGPRODUCTIONDETAIL (STOPE BREAKING, deduplicated by gang).
    # LEN(CREWNO)>=8 matches the filter used by get_rands_per_sqm / get_gang_detail so
    # the KPI denominator is consistent with every breakdown tab.
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
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              {sf}
        ) AS q
        GROUP BY section, period, gang
    """)
    if sqm_df.empty:
        return {}
    sqm_df["period"] = sqm_df["period"].astype(str)

    # Scope bonus to the SAME Stope Breaking (section, period, gang) keys as total_sqm above —
    # _get_participants_bonus() on its own returns every gang type, which would silently mix
    # Stope Cleaning/Centre Gully/etc. bonus into a KPI that's supposed to be Stope Breaking
    # only (matching the m² figure right next to it, and every other total on this page).
    # Merge-on-keys pattern already used correctly elsewhere (get_section_ranking,
    # get_rands_per_sqm). The `gangtype` parameter is intentionally NOT applied here — the
    # headline KPIs are unconditionally Stope Breaking, independent of the Gang Types filter
    # (which instead scopes the detail tabs); see queries.py docs for why.
    bonus_df = _get_participants_bonus(period_from, period_to, section)
    sb_keys = sqm_df[["section", "period", "gang"]].drop_duplicates()
    if not bonus_df.empty:
        bonus_sb = sb_keys.merge(
            bonus_df[["section", "period", "gang",
                      "total_bonus", "total_safety_bonus", "total_driller_bonus", "total_stm_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        for col in ["total_bonus", "total_safety_bonus", "total_driller_bonus", "total_stm_bonus"]:
            bonus_sb[col] = bonus_sb[col].fillna(0).astype(float)
    else:
        bonus_sb = sb_keys.copy()
        for col in ["total_bonus", "total_safety_bonus", "total_driller_bonus", "total_stm_bonus"]:
            bonus_sb[col] = 0.0

    total_sqm           = float(sqm_df["sqm"].sum())
    total_bonus         = float(bonus_sb["total_bonus"].sum())
    total_safety_bonus  = float(bonus_sb["total_safety_bonus"].sum())
    total_driller_bonus = float(bonus_sb["total_driller_bonus"].sum())
    total_stm_bonus     = float(bonus_sb["total_stm_bonus"].sum())
    gang_count          = int(sqm_df.groupby(["section", "gang"]).ngroups)
    avg_sqm             = float(sqm_df["sqm"].mean())
    r_per_sqm           = total_bonus / total_sqm if total_sqm > 0 else 0
    periods_seen        = sqm_df["period"].nunique()

    # Prior-period comparison (best-effort — None fields if prior data unavailable)
    prior_total_sqm = prior_total_bonus = prior_r_per_sqm = None
    try:
        pr_from, pr_to = _prior_period_range(period_from, period_to)
        pr_sqm_df = read_sql(f"""
            SELECT section, period, gang, MAX(sqm) AS sqm
            FROM (
                SELECT
                    LTRIM(RTRIM(SECTION)) AS section,
                    LTRIM(RTRIM(PERIOD))  AS period,
                    LTRIM(RTRIM(GANG))    AS gang,
                    TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm
                FROM [GANGPRODUCTIONDETAIL]
                WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
                  AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {pr_from} AND {pr_to}
                  AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
                  {sf}
            ) AS q
            GROUP BY section, period, gang
        """)
        pr_bonus_df     = _get_participants_bonus(pr_from, pr_to, section)
        prior_total_sqm = float(pr_sqm_df["sqm"].sum()) if not pr_sqm_df.empty else 0.0
        if not pr_sqm_df.empty and not pr_bonus_df.empty:
            pr_keys = pr_sqm_df[["section", "period", "gang"]].drop_duplicates()
            pr_bonus_sb = pr_keys.merge(
                pr_bonus_df[["section", "period", "gang", "total_bonus"]],
                on=["section", "period", "gang"], how="left"
            )
            prior_total_bonus = float(pr_bonus_sb["total_bonus"].fillna(0).sum())
        else:
            prior_total_bonus = 0.0
        prior_r_per_sqm = prior_total_bonus / prior_total_sqm if prior_total_sqm > 0 else 0.0
    except Exception:
        pass  # prior comparison is non-critical; don't break the main KPI response

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
        "prior_total_sqm":     round(prior_total_sqm,  0)  if prior_total_sqm  is not None else None,
        "prior_total_bonus":   round(prior_total_bonus, 2)  if prior_total_bonus is not None else None,
        "prior_r_per_sqm":     round(prior_r_per_sqm,  2)  if prior_r_per_sqm  is not None else None,
    }


# Real GANGTYPE values this dashboard's KPIs/scope cover (verified fresh against
# GANGPRODUCTIONDETAIL — ANCILLARY appeared in an old dropdown but never in real data).
SCOPE_GANGTYPES = ["STOPE BREAKING", "STOPE CLEANING", "CENTRE GULLY", "STOPING", "SURFACE ASSISTANT"]


def get_scope_note(period_from: int, period_to: int) -> dict:
    """What's included/excluded from this dashboard's totals, with a real excluded figure.

    Stoping Miner Bonus (policy REV10, JB_202606_STPMINER_REV10) is a genuinely separate
    incentive scheme, calculated and paid through MINERSEARN<period> tables rather than
    PARTICIPANTSDETAIL/GANGPRODUCTIONDETAIL, and was never surfaced anywhere in this app
    before. Verified before using it here: EMPLOYEEPRODUCTIONBONUS = EMPLOYEEEFFICIENCYBONUS +
    EMPLOYEESAFETYBONUS + EMPLOYEESWEEPINGSBONUS exactly on every sampled real row (not an
    assumed formula); a Miner genuinely gets more than one row per period when supervising more
    than one team, and SUMMING those rows is correct — not a duplicate-row bug like
    PARTICIPANTSDETAIL's GANGLINKEARN fan-out — confirmed against the policy's own "the Miner
    will receive the sum of the Teams that are supervised" rule (the summed per-employee max
    stays under the scheme's R150,000/month cap, as expected). EMPLOYEETOTALBONUS is always 0 in
    MINERSEARN (unpopulated), which is why relying on it alone (as done earlier in this
    project's development) wrongly concluded the scheme had no backing data.
    """
    periods = [p["value"] for p in get_available_periods() if period_from <= p["value"] <= period_to]
    periods.sort()

    miner_total = 0.0
    periods_found = 0
    exists_df = read_sql("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME LIKE 'MINERSEARN2%'")
    existing_tables = set(exists_df["TABLE_NAME"].str.upper()) if not exists_df.empty else set()
    for p in periods:
        tbl = f"MINERSEARN{p}"
        if tbl.upper() not in existing_tables:
            continue
        periods_found += 1
        df = read_sql(f"SELECT SUM(TRY_CAST(EMPLOYEEPRODUCTIONBONUS AS FLOAT)) AS total FROM [{tbl}]")
        if not df.empty and df["total"].iloc[0] is not None:
            miner_total += float(df["total"].iloc[0])

    return {
        "included_gangtypes": SCOPE_GANGTYPES,
        "excluded_miner_total": round(miner_total, 2),
        "excluded_miner_periods": periods_found,
    }


def get_production_trend(period_from: int, period_to: int,
                          section: str = "ALL") -> dict:
    """Monthly total adjusted m² (STOPE BREAKING) by section — for trend chart."""
    sf = _section_filter(section)
    # LEN(CREWNO)>=8 added to match get_kpi_summary so the trend total equals the KPI total.
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


def get_pre_adj_trend(period_from: int, period_to: int,
                      section: str = "ALL") -> dict:
    """Monthly Pre-Adj m² (SUM of WORKPLACETOTALSQM per gang) by section.

    GANGPRODUCTIONDETAIL can carry multiple rows for the same (gang, workplace) in a
    period — PRODUCTIONEARN has one row per production activity/blast, and WORKPLACETOTALSQM
    repeats identically across them. We MAX() per (gang, workplace) first to collapse those
    duplicates, then SUM across the gang's genuinely distinct workplaces — otherwise a
    workplace with N activity rows gets counted N times and inflates Pre-Adj vs Adj.
    """
    sf = _section_filter(section)
    df = read_sql(f"""
        SELECT section, period, gang, SUM(wp_sqm) AS sqm
        FROM (
            SELECT section, period, gang, workplace, MAX(wp_sqm) AS wp_sqm
            FROM (
                SELECT
                    LTRIM(RTRIM(SECTION))   AS section,
                    LTRIM(RTRIM(PERIOD))    AS period,
                    LTRIM(RTRIM(GANG))      AS gang,
                    LTRIM(RTRIM(WORKPLACE)) AS workplace,
                    LTRIM(RTRIM(CREWNO))    AS crewno,
                    TRY_CAST(WORKPLACETOTALSQM AS FLOAT) AS wp_sqm
                FROM [GANGPRODUCTIONDETAIL]
                WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
                  AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
                  AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
                  {sf}
            ) AS raw
            GROUP BY section, period, gang, workplace
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
    """Total bonus by GANGTYPE and period — from PARTICIPANTSDETAIL with breakdown.

    total_bonus = SUM(EMPLOYEESTOPETEAMBONUS) only — see _get_participants_bonus docstring
    for the per-employee reconciliation against EMPLOYEETOTALBONUS that verifies this.
    """
    sf = _section_filter(section)
    df = read_sql(f"""
        SELECT
            LTRIM(RTRIM(GANGTYPE))  AS gangtype,
            LTRIM(RTRIM(PERIOD))    AS period,
            SUM(ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0)) AS stm_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEESAFETYBONUS    AS FLOAT), 0)) AS safety_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEEDRILLERBONUS   AS FLOAT), 0)) AS driller_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0)) AS total_bonus
        FROM {_participants_dedup_sql(f"TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}")} AS pd
        WHERE LTRIM(RTRIM(GANG))   != 'xxx'
          AND LTRIM(RTRIM(CREWNO)) != '-'
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


def get_section_rsqm_trend(period_from: int, period_to: int) -> dict:
    """Per-section, per-period STOPE BREAKING R/m², for the Overview Executive Summary's
    "Monthly Bonus Cost per m² by Section" chart. get_bonus_by_section() is NOT usable here
    on its own -- it sums bonus across every gang type, not just Stope Breaking, which would
    overstate R/m² against a Stope-Breaking-only m² denominator. Same merge-on-(section,
    period,gang)-keys scoping already used in get_section_ranking/get_rands_per_sqm.
    """
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
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
        ) AS q
        GROUP BY section, period, gang
    """)
    if sqm_df.empty:
        return {"sections": [], "rows": []}
    sqm_df["period"] = sqm_df["period"].astype(str)

    bonus_df = _get_participants_bonus(period_from, period_to, "ALL")
    if not bonus_df.empty:
        merged = sqm_df.merge(
            bonus_df[["section", "period", "gang", "total_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        merged["total_bonus"] = merged["total_bonus"].fillna(0).astype(float)
    else:
        merged = sqm_df.copy()
        merged["total_bonus"] = 0.0

    pivot = merged.groupby(["section", "period"]).agg(
        sqm=("sqm", "sum"), bonus=("total_bonus", "sum")
    ).reset_index()

    period_order = sorted(pivot["period"].unique())
    sections = sorted(pivot["section"].unique())
    pivot_idx = pivot.set_index(["section", "period"])

    rows = []
    for p in period_order:
        row = {"period": _period_label(int(p)), "sections": {}}
        for s in sections:
            key = (s, p)
            if key in pivot_idx.index:
                sqm   = float(pivot_idx.loc[key, "sqm"])
                bonus = float(pivot_idx.loc[key, "bonus"])
                row["sections"][s] = {
                    "sqm": round(sqm, 0),
                    "bonus": round(bonus, 2),
                    "r_per_sqm": round(bonus / sqm, 2) if sqm > 0 else None,
                }
        rows.append(row)
    return {"sections": sections, "rows": rows}


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
    # Group by gang (not crewno) so each gang appears once per period.
    # This prevents bonus double-counting when a gang has multiple CREWNO rows
    # in GANGPRODUCTIONDETAIL: the PARTICIPANTSDETAIL bonus merge is on gang key,
    # so multiple crewno rows for the same gang would each receive the full gang
    # bonus and inflate the range-bucket totals.
    df = read_sql(f"""
        SELECT
            LTRIM(RTRIM(SECTION)) AS section,
            LTRIM(RTRIM(PERIOD))  AS period,
            LTRIM(RTRIM(GANG))    AS gang,
            MAX(TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT)) AS sqm,
            MAX(TRY_CAST(GANGLABOUR           AS FLOAT)) AS labour
        FROM [GANGPRODUCTIONDETAIL]
        WHERE TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
          AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
          AND UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
          {sf}
        GROUP BY
            LTRIM(RTRIM(SECTION)),
            LTRIM(RTRIM(PERIOD)),
            LTRIM(RTRIM(GANG))
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
        crew_count    = ("gang",               "count"),
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
    period_pivot = df.groupby(["period", "sqm_range"])["gang"].nunique().reset_index()
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
    Tshepong-specific bonus columns: efficiency, drill, sweeping penalty, safety (+ prod as subtotal).
    Safety indicators: LTI, Dressing, Fatal (not single GANGSAFETYIND).
    """
    sf = _section_filter(section)
    gf = _gangtype_filter(gangtype)

    # WORKPLACETOTALSQM (startup_sqm) can repeat across multiple activity rows for the
    # same (gang, workplace) — dedupe per workplace with an inner MAX() before summing,
    # otherwise a workplace with several activity rows inflates startup_sqm vs adj_sqm.
    df = read_sql(f"""
        SELECT
            section, period, gang, crewno, gangtype,
            MAX(adj_sqm)     AS adj_sqm,
            SUM(wp_sqm)      AS startup_sqm,
            MAX(labour)      AS labour,
            MAX(efficiency)  AS efficiency,
            MAX(efficiency_bonus) AS efficiency_bonus,
            MAX(drill_bonus) AS drill_bonus,
            MAX(sweep_penalty) AS sweep_penalty,
            MAX(safety_bonus)AS safety_bonus,
            MAX(lti_ind)     AS lti_ind,
            MAX(dress_ind)   AS dress_ind,
            MAX(fatal_ind)   AS fatal_ind
        FROM (
            SELECT
                section, period, gang, crewno, gangtype, workplace,
                MAX(adj_sqm)      AS adj_sqm,
                MAX(wp_sqm)       AS wp_sqm,
                MAX(labour)       AS labour,
                MAX(efficiency)   AS efficiency,
                MAX(efficiency_bonus) AS efficiency_bonus,
                MAX(drill_bonus)  AS drill_bonus,
                MAX(sweep_penalty)  AS sweep_penalty,
                MAX(safety_bonus) AS safety_bonus,
                MAX(lti_ind)      AS lti_ind,
                MAX(dress_ind)    AS dress_ind,
                MAX(fatal_ind)    AS fatal_ind
            FROM (
                SELECT
                    LTRIM(RTRIM(SECTION))   AS section,
                    LTRIM(RTRIM(PERIOD))    AS period,
                    LTRIM(RTRIM(GANG))      AS gang,
                    LTRIM(RTRIM(CREWNO))    AS crewno,
                    LTRIM(RTRIM(GANGTYPE))  AS gangtype,
                    LTRIM(RTRIM(WORKPLACE)) AS workplace,
                    TRY_CAST(GANGTOTALSQMADJUSTED   AS FLOAT) AS adj_sqm,
                    TRY_CAST(WORKPLACETOTALSQM      AS FLOAT) AS wp_sqm,
                    TRY_CAST(GANGLABOUR             AS FLOAT) AS labour,
                    TRY_CAST(GANGEFFICIENCY         AS FLOAT) AS efficiency,
                    TRY_CAST(GANGFINALBREAKBONUS     AS FLOAT) AS efficiency_bonus,
                    TRY_CAST(GANGDRILLERBONUS        AS FLOAT) AS drill_bonus,
                    TRY_CAST(GANGFINALSWEEPINGSBONUS AS FLOAT) AS sweep_penalty,
                    TRY_CAST(GANGFINALSAFETYBONUS    AS FLOAT) AS safety_bonus,
                    TRY_CAST(GANGLTIIND             AS FLOAT) AS lti_ind,
                    TRY_CAST(GANGDRESSINGIND         AS FLOAT) AS dress_ind,
                    TRY_CAST(GANGFATALIND            AS FLOAT) AS fatal_ind
                FROM [GANGPRODUCTIONDETAIL]
                WHERE TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
                  AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
                  {sf} {gf}
            ) AS raw
            GROUP BY section, period, gang, crewno, gangtype, workplace
        ) AS byworkplace
        GROUP BY section, period, gang, crewno, gangtype
        ORDER BY section, period, gang
    """)
    if df.empty:
        return []

    df["period"] = df["period"].astype(str)

    # Multiply per-person bonus columns by labour to get gang-level totals
    for bc in ["efficiency_bonus", "drill_bonus", "sweep_penalty", "safety_bonus"]:
        df[bc] = df[bc].fillna(0) * df["labour"].fillna(0)

    df["efficiency"] = df["efficiency"].fillna(0)
    df["sqm_range"]  = df["adj_sqm"].apply(lambda x: _sqm_range_label(float(x or 0)))

    records = []
    for _, row in df.iterrows():
        total_bonus = (float(row["efficiency_bonus"]  or 0) +
                       float(row["drill_bonus"]   or 0) +
                       float(row["sweep_penalty"]   or 0) +
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
            "efficiency_bonus":  round(float(row["efficiency_bonus"] or 0), 2),
            "drill_bonus":  round(float(row["drill_bonus"] or 0), 2),
            "sweep_penalty":  round(float(row["sweep_penalty"] or 0), 2),
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

    # Dedupe WORKPLACETOTALSQM per (gang, workplace) before summing — see get_pre_adj_trend.
    raw_df = read_sql(f"""
        SELECT period, gang,
               MAX(adj_sqm) AS adj_sqm,
               SUM(wp_sqm)  AS startup_sqm
        FROM (
            SELECT period, gang, workplace,
                   MAX(adj_sqm) AS adj_sqm,
                   MAX(wp_sqm)  AS wp_sqm
            FROM (
                SELECT
                    LTRIM(RTRIM(PERIOD))    AS period,
                    LTRIM(RTRIM(GANG))      AS gang,
                    LTRIM(RTRIM(WORKPLACE)) AS workplace,
                    TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS adj_sqm,
                    TRY_CAST(WORKPLACETOTALSQM    AS FLOAT) AS wp_sqm
                FROM [GANGPRODUCTIONDETAIL]
                WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
                  AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
                  AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
                  {sf}
            ) AS raw
            GROUP BY period, gang, workplace
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

    # Scope bonus to the same STOPE BREAKING (period, gang) keys as adj_sqm/startup_sqm —
    # _get_participants_bonus() on its own returns every gang type, which would silently mix
    # STOPE CLEANING/CENTRE GULLY/etc. bonus into a chart and table both labelled "Stope
    # Breaking" (see the chart title above). Match the merge-on-gang-keys pattern already
    # used correctly in get_section_ranking/get_forecast_data. raw_df has no section column,
    # so key on (period, gang) — gang codes are section-prefixed and already unique.
    bonus_df = _get_participants_bonus(period_from, period_to, section)
    if not bonus_df.empty:
        sb_keys = raw_df[["period", "gang"]].drop_duplicates()
        bonus_sb = sb_keys.merge(
            bonus_df[["period", "gang", "total_bonus", "total_stm_bonus",
                      "total_safety_bonus", "total_driller_bonus"]],
            on=["period", "gang"], how="left"
        )
        for col in ["total_bonus", "total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]:
            bonus_sb[col] = bonus_sb[col].fillna(0).astype(float)
        bonus_agg = bonus_sb.groupby("period")[
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

    # Pre-Adj SQM: dedupe WORKPLACETOTALSQM per (gang, workplace) first — GANGPRODUCTIONDETAIL
    # can carry multiple activity rows per workplace/period, each repeating the same
    # WORKPLACETOTALSQM value, which would otherwise inflate this total vs the Adj figure.
    wp_df = read_sql(f"""
        SELECT period, SUM(startup_sqm) AS startup_sqm
        FROM (
            SELECT
                LTRIM(RTRIM(PERIOD)) AS period,
                LTRIM(RTRIM(GANG))   AS gang,
                SUM(wp_sqm) AS startup_sqm
            FROM (
                SELECT
                    LTRIM(RTRIM(PERIOD))    AS period,
                    LTRIM(RTRIM(GANG))      AS gang,
                    LTRIM(RTRIM(WORKPLACE)) AS workplace,
                    MAX(TRY_CAST(WORKPLACETOTALSQM AS FLOAT)) AS wp_sqm
                FROM [GANGPRODUCTIONDETAIL]
                WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
                  AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
                  AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
                  {sf}
                GROUP BY LTRIM(RTRIM(PERIOD)), LTRIM(RTRIM(GANG)), LTRIM(RTRIM(WORKPLACE))
            ) AS byworkplace
            GROUP BY period, gang
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
      WPTOTALM2 - WPPRETOTALM2 → extram2 / m2_variance

    DIP_FACTOR (the Steep Stope geometry uplift, policy §5.10/§5.11) is deliberately not
    exposed here — it's already fully baked into WPTOTALM2/totalm2 (WPTOTALM2 = WPPRETOTALM2
    × DIP_FACTOR, confirmed exactly against real data), so showing it as its own column would
    just be restating information already visible in Total m², not new information.
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
            "totalm2":          round(float(row["totalm2"]           or 0), 2),
            "m2_variance":      round(float(row["m2_variance"]       or 0), 2),
        })
    return records


def get_section_ranking(period_from: int, period_to: int) -> list[dict]:
    """
    Per-section STOPE BREAKING summary for the Section Performance Ranking table.
    Sorted by R/m² descending (highest bonus earned per m² first).
    SQM from GANGPRODUCTIONDETAIL; bonus from PARTICIPANTSDETAIL (canonical).
    """
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
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
        ) AS q
        GROUP BY section, period, gang
    """)
    if sqm_df.empty:
        return []

    sqm_df["sqm"] = sqm_df["sqm"].fillna(0).astype(float)

    sec_agg = sqm_df.groupby("section").agg(
        total_sqm  = ("sqm",  "sum"),
        gang_count = ("gang", "nunique"),
    ).reset_index()

    # Scope bonus to the same STOPE BREAKING (section, period, gang) keys as total_sqm —
    # _get_participants_bonus() on its own returns every gang type for the section, which
    # would silently mix STOPE CLEANING/CENTRE GULLY/etc. bonus into a ratio the UI labels
    # "Stope Breaking only" (see the section-header caption). Match the merge-on-gang-keys
    # pattern already used correctly in get_section_ranking/get_forecast_data.
    bonus_df = _get_participants_bonus(period_from, period_to)
    if not bonus_df.empty:
        sb_keys = sqm_df[["section", "period", "gang"]].drop_duplicates()
        bonus_sb = sb_keys.merge(
            bonus_df[["section", "period", "gang", "total_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        bonus_sb["total_bonus"] = bonus_sb["total_bonus"].fillna(0).astype(float)
        sec_bonus = bonus_sb.groupby("section").agg(
            total_bonus = ("total_bonus", "sum")
        ).reset_index()
    else:
        sec_bonus = pd.DataFrame(columns=["section", "total_bonus"])

    merged = sec_agg.merge(sec_bonus, on="section", how="left")
    merged["total_bonus"] = merged["total_bonus"].fillna(0).astype(float)
    merged["r_per_sqm"] = merged.apply(
        lambda r: r["total_bonus"] / r["total_sqm"] if r["total_sqm"] > 0 else 0.0, axis=1
    )
    merged["avg_sqm_per_gang"] = merged.apply(
        lambda r: r["total_sqm"] / r["gang_count"] if r["gang_count"] > 0 else 0.0, axis=1
    )
    merged = merged.sort_values("r_per_sqm", ascending=False).reset_index(drop=True)

    return [
        {
            "section":          str(row["section"]),
            "total_sqm":        round(float(row["total_sqm"]), 0),
            "total_bonus":      round(float(row["total_bonus"]), 2),
            "r_per_sqm":        round(float(row["r_per_sqm"]), 2),
            "gang_count":       int(row["gang_count"]),
            "avg_sqm_per_gang": round(float(row["avg_sqm_per_gang"]), 1),
        }
        for _, row in merged.iterrows()
    ]


# Policy defaults for the Bonus Policy Simulator's real levers — see the "Thibakotsi Stoping
# Incentive Scheme Cat 4-8" policy, JB_202603_STPTEAM_REV04 (effective March 2026): §5.3/§5.13
# netting, §5.12 B-Reef stoping width, §6.1.3-6.1.6 safety ladder — and the empirically-verified
# netting/B-Reef-SW multiplier found in GANGFULLSTOPINGBONUS → GANGFINALBREAKBONUS.
#
# NOTE on the old m² tier escalator: REV02 (June 2024, the policy this simulator was
# originally built against) had a §5.3 clause paying an extra 5/10/20/30/50% on top of the
# qualifying bonus for teams breaking 300+/400+/500+/600+/700+m². That clause does not exist
# in REV04 — the current policy has no m² tier escalator at all. This matches the database:
# checked GANGFULLSTOPINGBONUS → GANGFINALBREAKBONUS across 39 real gangs spanning every old
# tier band (100m² to 600m²) and the ratio equals netting × B-Reef-SW exactly regardless of
# which band a gang falls in — there is no trace of a tier multiplier being applied. So this
# simulator no longer includes one; it would model a lever that doesn't exist in current policy.
#
# NOTE on Steep Stope (REV04 §5.10/§5.11, new vs REV02): panels with a dip of 35-44° get +10%
# added to their m², and ≥45° get +20% — confirmed directly against PRODUCTIONWPDETAIL, where
# WPTOTALM2 = WPPRETOTALM2 × DIP_FACTOR exactly (e.g. 279 × 1.2 = 334.8). This is a real,
# active mechanism, but unlike netting/SW it operates on the square-metre figure BEFORE the
# efficiency/bonus-table lookup, not as a multiplier on the final Rand bonus. That means it's
# already fully reflected in GANGTOTALSQMADJUSTED (and therefore in every bonus figure derived
# from it) everywhere in this dashboard — no separate lever is needed or possible here, because
# modelling "what if we changed the Steep Stope %" would require re-running the changed m²
# through the underlying bonus-table lookup, which lives in an external calc engine this
# database doesn't expose (only a 2018-dated and a 2026-dated snapshot of the printed tables).
BONUS_POLICY_DEFAULTS = {
    "entry_threshold": 14.0,          # §5.4 — m²/empl qualifying gate
    "netting_pct":     20.0,          # §5.13.4 — WORKPLACENETTINGRATE observed as 1.2
    "sw_pct":          10.0,          # §5.12 — B-Reef stoping width ≥1.60m; FACTORS.MINEBREEF_SW_RATE observed as 1.1
    "safety_pct": {                   # §6.1.3-6.1.6 — ladder by incident indicator (NOT the
                                       # §6.1.1/§6.1.2 Physical Conditions Rating — see note below)
        "clean":    25.0,
        "dressing":  0.0,
        "lti":     -25.0,
        "fatal":  -100.0,
    },
}


def _pull_stope_breaking_rows(period_from: int, period_to: int, section: str = "ALL") -> pd.DataFrame:
    """One row per STOPE BREAKING (section, period, gang) in the selected range, carrying the
    GANGPRODUCTIONDETAIL per-gang ESTIMATE of every bonus component (per-person rate x
    GANGLABOUR) together with the real PARTICIPANTSDETAIL totals for the same gang keys
    (total_stm_bonus / total_safety_bonus / total_driller_bonus / total_awop_bonus).

    Shared by the Bonus Policy Simulator (get_bonus_rule_data) and the Bonus Analysis
    breakdown tables (get_bonus_composition / get_bonus_outcomes / get_policy_discrepancies)
    so every "what the bonus is built from" figure starts from identical ingredients. No
    anchoring is applied here -- the two consumers anchor differently (see each one), so the
    raw ingredients are kept in one place and each caller documents its own step.

    Adj SQM from GANGTOTALSQMADJUSTED; pre-adj from SUM(WORKPLACETOTALSQM) per gang/period.
    sweep_penalty is signed (usually <= 0) -- GANGFINALSWEEPINGSBONUS is a penalty deduction
    for failing the sweepings standard (policy REV04 s5.14), not a bonus, despite the name.
    """
    sf = _section_filter(section)
    # Qualified with g. -- this query LEFT JOINs PRODUCTIONWPDETAIL (alias p), which also has
    # a SECTION column, so the unqualified filter from _section_filter() would be ambiguous.
    sf_g = sf.replace("SECTION", "g.SECTION") if sf else ""

    # Dedupe WORKPLACETOTALSQM (and the other repeating gang-level columns) per (gang,
    # workplace) before summing/collapsing -- see get_pre_adj_trend for why this matters.
    df = read_sql(f"""
        SELECT
            section, period, gang, crewno,
            MAX(adj_sqm)     AS sqm,
            SUM(wp_sqm)      AS sqm_preadj,
            MAX(labour)      AS labour,
            MAX(efficiency)  AS efficiency,
            MAX(efficiency_bonus) AS efficiency_bonus,
            MAX(drill_bonus) AS drill_bonus,
            MAX(sweep_penalty) AS sweep_penalty,
            MAX(safety_bonus)AS safety_bonus,
            MAX(netting_rate)AS netting_rate,
            MAX(sw_factor)   AS sw_factor,
            MAX(lti_ind)     AS lti_ind,
            MAX(dress_ind)   AS dress_ind,
            MAX(fatal_ind)   AS fatal_ind,
            MAX(is_wideraise)AS is_wideraise
        FROM (
            SELECT
                section, period, gang, crewno, workplace,
                MAX(adj_sqm)      AS adj_sqm,
                MAX(wp_sqm)       AS wp_sqm,
                MAX(labour)       AS labour,
                MAX(efficiency)   AS efficiency,
                MAX(efficiency_bonus) AS efficiency_bonus,
                MAX(drill_bonus)  AS drill_bonus,
                MAX(sweep_penalty)  AS sweep_penalty,
                MAX(safety_bonus) AS safety_bonus,
                MAX(netting_rate) AS netting_rate,
                MAX(sw_factor)    AS sw_factor,
                MAX(lti_ind)      AS lti_ind,
                MAX(dress_ind)    AS dress_ind,
                MAX(fatal_ind)    AS fatal_ind,
                MAX(is_wideraise) AS is_wideraise
            FROM (
                SELECT
                    LTRIM(RTRIM(g.SECTION))   AS section,
                    LTRIM(RTRIM(g.PERIOD))    AS period,
                    LTRIM(RTRIM(g.GANG))      AS gang,
                    LTRIM(RTRIM(g.CREWNO))    AS crewno,
                    LTRIM(RTRIM(g.WORKPLACE)) AS workplace,
                    TRY_CAST(g.GANGTOTALSQMADJUSTED    AS FLOAT) AS adj_sqm,
                    TRY_CAST(g.WORKPLACETOTALSQM       AS FLOAT) AS wp_sqm,
                    TRY_CAST(g.GANGLABOUR              AS FLOAT) AS labour,
                    TRY_CAST(g.GANGEFFICIENCY          AS FLOAT) AS efficiency,
                    TRY_CAST(g.GANGFINALBREAKBONUS     AS FLOAT) AS efficiency_bonus,
                    TRY_CAST(g.GANGDRILLERBONUS        AS FLOAT) AS drill_bonus,
                    TRY_CAST(g.GANGFINALSWEEPINGSBONUS AS FLOAT) AS sweep_penalty,
                    TRY_CAST(g.GANGFINALSAFETYBONUS    AS FLOAT) AS safety_bonus,
                    TRY_CAST(g.WORKPLACENETTINGRATE    AS FLOAT) AS netting_rate,
                    TRY_CAST(g.B_REEF_SW_FACTOR        AS FLOAT) AS sw_factor,
                    TRY_CAST(g.GANGLTIIND              AS FLOAT) AS lti_ind,
                    TRY_CAST(g.GANGDRESSINGIND         AS FLOAT) AS dress_ind,
                    TRY_CAST(g.GANGFATALIND            AS FLOAT) AS fatal_ind,
                    CASE WHEN UPPER(LTRIM(RTRIM(p.WORKPLACERATETYPE))) = 'WIDE RAISE BONUS'
                         THEN 1 ELSE 0 END AS is_wideraise
                FROM [GANGPRODUCTIONDETAIL] g
                LEFT JOIN [PRODUCTIONWPDETAIL] p
                       ON LTRIM(RTRIM(g.SECTION))   = LTRIM(RTRIM(p.SECTION))
                      AND LTRIM(RTRIM(g.PERIOD))    = LTRIM(RTRIM(p.PERIOD))
                      AND LTRIM(RTRIM(g.WORKPLACE)) = LTRIM(RTRIM(p.WORKPLACE))
                WHERE UPPER(LTRIM(RTRIM(g.GANGTYPE))) = 'STOPE BREAKING'
                  AND TRY_CAST(g.PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
                  AND LEN(LTRIM(RTRIM(g.CREWNO))) >= 8
                  {sf_g}
            ) AS raw
            GROUP BY section, period, gang, crewno, workplace
        ) AS byworkplace
        GROUP BY section, period, gang, crewno
    """)
    if df.empty:
        return df

    df["period"]    = df["period"].astype(str)
    df["sqm"]       = df["sqm"].fillna(0).astype(float)
    df["sqm_preadj"]= df["sqm_preadj"].fillna(0).astype(float)
    df["efficiency"]= df["efficiency"].fillna(0).astype(float)
    df["had_wideraise"] = df["is_wideraise"].fillna(0).astype(float) > 0
    labour = df["labour"].fillna(0).astype(float)

    # GANGPRODUCTIONDETAIL's bonus columns are per-person rates; x GANGLABOUR gives the
    # production-formula ESTIMATE of the gang's payout. That estimate measurably diverges from
    # real payroll (GANGLABOUR is a rounded-up average headcount: one real period range had
    # 3,650 "labour units" against 4,220 real employee-period records in PARTICIPANTSDETAIL for
    # the identical gangs, a 15.6% headcount-basis gap), which is why callers anchor these to
    # the real PARTICIPANTSDETAIL totals merged below instead of showing them directly.
    for bc in ["efficiency_bonus", "drill_bonus", "sweep_penalty", "safety_bonus"]:
        df[bc] = df[bc].fillna(0).astype(float) * labour

    # Real PARTICIPANTSDETAIL totals for the same gang keys. Driller and Safety are tracked
    # independently there (EMPLOYEEDRILLERBONUS, EMPLOYEESAFETYBONUS); Efficiency, Safety and
    # Sweep are bundled into one real paid figure (EMPLOYEESTOPETEAMBONUS / "STM" -- confirmed
    # empirically on 541/541 Stope Breaking gang-periods Jul-25..Apr-26: the raw
    # GANGPRODUCTIONBONUS column equals GANGFINALBREAKBONUS + GANGFINALSWEEPINGSBONUS +
    # GANGFINALSAFETYBONUS exactly, driller excluded). EMPLOYEEAWOPPENALTY is already netted
    # inside STM (raw PARTICIPANTSEARN: EMPLOYEETOTALBONUS equals STM, never STM + AWOP, on
    # every AWOP row checked) -- it is carried as a reference figure only.
    participants = _get_participants_bonus(period_from, period_to, section)
    real_cols = ["total_stm_bonus", "total_safety_bonus", "total_driller_bonus", "total_awop_bonus"]
    if not participants.empty:
        keys = df[["section", "period", "gang"]].drop_duplicates()
        real = keys.merge(participants[["section", "period", "gang"] + real_cols],
                          on=["section", "period", "gang"], how="left")
    else:
        real = df[["section", "period", "gang"]].drop_duplicates()
        for col in real_cols:
            real[col] = 0.0
    for col in real_cols:
        real[col] = real[col].fillna(0).astype(float)
    df = df.merge(real, on=["section", "period", "gang"], how="left")
    for col in real_cols:
        df[col] = df[col].fillna(0).astype(float)

    # netting_rate / sw_factor come through as 1.0 (no uplift) or the applied multiplier
    # (1.2 netting, 1.1 B-Reef SW) -- default missing values to 1.0 (no uplift), never 0, to
    # keep the decomposition division-safe.
    df["netting_rate"] = df["netting_rate"].fillna(1.0).astype(float).replace(0, 1.0)
    df["sw_factor"]    = df["sw_factor"].fillna(1.0).astype(float).replace(0, 1.0)
    df["lti_ind"]      = df["lti_ind"].fillna(0).astype(float)
    df["dress_ind"]    = df["dress_ind"].fillna(0).astype(float)
    df["fatal_ind"]    = df["fatal_ind"].fillna(0).astype(float)
    return df


def _safety_rung(row) -> str:
    if row["fatal_ind"] >= 1:
        return "fatal"
    if row["lti_ind"] >= 1:
        return "lti"
    if row["dress_ind"] >= 1:
        return "dressing"
    return "clean"


def _decompose_bonus_row(row, safety_defaults: dict) -> tuple[float, float, bool]:
    """Return (raw_qualifying, safety_pct, gated) for one anchored Stope Breaking row.

    safety_pct is the written-ladder rate for the gang's real incident indicators (policy
    REV04 s6.1.3-6.1.6) -- independent of whether the efficiency bonus happens to be gated
    to zero, since a gang can be efficiency-gated while still having a genuine non-clean
    safety record and a real, separate safety figure. The Simulator uses it as the "old %"
    baseline to ratio-scale FROM when a Safety % lever changes.
    `gated` only describes whether raw_qualifying (the efficiency component) can be
    reconstructed -- a gang already at R0 efficiency has no signal to recover a base from.

    combined_mult = netting_rate x sw_factor only, NO safety term. Netting and B-Reef SW are
    multipliers on the efficiency bonus (WORKPLACENETTINGRATE 1.2 and B_REEF_SW_FACTOR 1.1
    multiply the pre-uplift efficiency estimate onto GANGFINALBREAKBONUS exactly); the safety
    ladder is NOT -- it is a separate additive component sitting inside STM alongside
    efficiency (GANGPRODUCTIONBONUS = efficiency + sweep + safety on every real row). An
    earlier version baked (1 + safety_pct/100) into combined_mult; that only reproduced the
    real number at the exact default by construction and diverged the moment a lever moved.
    (Note for anyone re-checking: real EMPLOYEESAFETYBONUS is ~20% of real STM for clean
    gangs -- that is the same thing as the written +25% of Efficiency, because STM already
    contains the safety piece: 0.25E / 1.25E = 20%. It is not a discrepancy with the ladder.)
    """
    rung = _safety_rung(row)
    safety_pct = safety_defaults[rung]

    actual_bonus = float(row["efficiency_bonus"])
    if actual_bonus <= 0:
        return 0.0, safety_pct, True  # gated: no signal to recover a base for this component

    combined_mult = float(row["netting_rate"]) * float(row["sw_factor"])
    if combined_mult <= 1e-9:
        return 0.0, safety_pct, True
    return actual_bonus / combined_mult, safety_pct, False


def _anchor_components(df: pd.DataFrame) -> pd.DataFrame:
    """Three-way anchor for the Bonus Analysis breakdown tables: scale the estimate's
    Efficiency / Safety / Sweep -- whose sum is the estimate of STM (GANGPRODUCTIONBONUS x
    labour; identity verified on 541/541 gang-periods) -- by one common ratio onto the real
    STM total, so anchored efficiency + safety + sweep = real STM exactly per gang-period and
    the relative split between the three is the production formula's own.

    This deliberately differs from get_bonus_rule_data()'s two-way anchor, which splits real
    STM across efficiency + sweep only (its 'efficiency' therefore carries the safety share
    inside it, and its total is efficiency + sweep). Both land on the same grand total; the
    Simulator's verified lever mechanics are left exactly as they were, and this tab shows
    the safety piece on its own line instead.

    Gated rows (no estimate signal to split by): the whole real STM is carried on efficiency,
    consistent with the Simulator's choice, so no rand is lost from the total.
    """
    est_total = df["efficiency_bonus"] + df["sweep_penalty"] + df["safety_bonus"]
    real_stm  = df["total_stm_bonus"]
    has_est   = est_total.abs() > 1e-9
    ratio     = (real_stm / est_total.where(has_est, 1.0)).where(has_est, 0.0)
    df["efficiency_bonus"] = (df["efficiency_bonus"] * ratio).where(has_est, real_stm)
    df["sweep_penalty"]    = (df["sweep_penalty"]    * ratio).where(has_est, 0.0)
    df["safety_bonus"]     = (df["safety_bonus"]     * ratio).where(has_est, 0.0)
    df["drill_bonus"]      = df["total_driller_bonus"]
    return df


def _composition_frame(period_from: int, period_to: int, section: str = "ALL") -> pd.DataFrame:
    """Anchored (three-way) Stope Breaking rows with the sequential efficiency build-up
    attached: raw_step -> x netting -> x B-Reef SW = efficiency_bonus (exact by construction
    for non-gated rows; gated rows carry their real efficiency unchanged with zero uplift
    attributed). Shared by get_bonus_composition / get_bonus_outcomes / get_policy_discrepancies
    so the Rand in each of those reconciles to the others by construction.
    """
    df = _pull_stope_breaking_rows(period_from, period_to, section)
    if df.empty:
        return df
    df = _anchor_components(df)
    safety_defaults = BONUS_POLICY_DEFAULTS["safety_pct"]
    decomposed = df.apply(lambda r: _decompose_bonus_row(r, safety_defaults), axis=1, result_type="expand")
    df["raw_qualifying"], df["safety_pct_tier"], df["gated"] = decomposed[0], decomposed[1], decomposed[2]
    gated = df["gated"]
    step0 = df["raw_qualifying"].where(~gated, df["efficiency_bonus"])
    step1 = (step0 * df["netting_rate"]).where(~gated, step0)
    step2 = (step1 * df["sw_factor"]).where(~gated, step1)
    df["raw_step"]      = step0
    df["netting_delta"] = step1 - step0
    df["sw_delta"]      = step2 - step1
    df["had_netting"]   = df["netting_rate"] > 1.0001
    df["had_sw"]        = df["sw_factor"]    > 1.0001
    df["safety_rung"]   = df.apply(_safety_rung, axis=1)
    return df


def get_bonus_rule_data(period_from: int, period_to: int, section: str = "ALL") -> dict:
    """Per-gang STOPE BREAKING bonus data for the Bonus Policy Simulator tab.

    Tshepong schema has four bonus components: efficiency, driller, sweeping penalty, safety.
    sweep_penalty is signed (usually ≤0) — GANGFINALSWEEPINGSBONUS is a penalty deduction for
    failing the sweepings standard (policy §5.14), not an actual bonus, despite the column name.
    Adj SQM from GANGTOTALSQMADJUSTED; pre-adj from SUM(WORKPLACETOTALSQM) per gang/period.

    IMPORTANT — dollar totals are anchored to PARTICIPANTSDETAIL, not GANGPRODUCTIONDETAIL:
    GANGPRODUCTIONDETAIL's bonus columns are per-person rates that, multiplied by GANGLABOUR
    (a rounded-up *average* headcount used for the efficiency calc), only ever produce an
    ESTIMATE of the gang's total payout — not the real amount paid to real individuals. That
    estimate was found to diverge from PARTICIPANTSDETAIL (the canonical payroll source, also
    what "Bonus Analysis"/Bonus by Gang Type is built from) by double digits of a percent: for
    one real period range, GANGLABOUR summed to 3,650 "labour units" against 4,220 real
    employee-period records in PARTICIPANTSDETAIL for the identical gangs. So every gang row
    here is re-anchored: Driller and Safety take PARTICIPANTSDETAIL's own EMPLOYEEDRILLERBONUS/
    EMPLOYEESAFETYBONUS totals directly (already isolated there); Efficiency and Sweep are
    bundled into one real figure in PARTICIPANTSDETAIL (EMPLOYEESTOPETEAMBONUS / "STM" —
    confirmed empirically: the raw GANGPRODUCTIONBONUS column equals efficiency + sweep +
    safety, driller excluded), so they're split back out of the real STM total using the
    GANGPRODUCTIONDETAIL estimate's own efficiency:sweep ratio as weights. This means the
    grand total this function returns matches "Bonus by Gang Type"'s Stope Breaking total for
    the same period/section exactly (both ultimately sum PARTICIPANTSDETAIL for the same gang
    keys) — only the split between components, and the lever mechanics, come from the
    GANGPRODUCTIONDETAIL-side estimate, because only it has the raw ingredients (efficiency,
    netting flag, B-Reef factor, safety indicators) the simulator's levers need.

    The (now anchored) Efficiency Bonus is decomposed into a lever-independent "raw_qualifying"
    amount so the simulator can recompute it under different policy parameters instead of just
    scaling the actual paid figure:
        efficiency_bonus_anchored = raw_qualifying × netting_mult × sw_mult × safety_mult
    netting_mult/sw_mult are empirically verified to multiply exactly onto the *estimate*
    (WORKPLACENETTINGRATE and B_REEF_SW_FACTOR multiply exactly onto GANGFULLSTOPINGBONUS to
    produce GANGFINALBREAKBONUS, checked across 39 gangs spanning 100m² to 600m² — also
    confirming there is no tier effect; see BONUS_POLICY_DEFAULTS comment above for why the
    old tier escalator is gone from both the current policy and this model). Applying those
    same verified ratios to the anchored dollar figure is an extrapolation, not something
    independently re-verified against real anchored data — flagged in the simulator UI.
    The entry-level gate is applied separately (a hard cutoff, not a multiplier), using the
    real GANGEFFICIENCY (m²/empl) figure, which the anchoring doesn't touch. Wide Raise
    workplaces (WORKPLACERATETYPE) are exempted from it per policy §5.4, confirmed against
    real low-efficiency Wide Raise gangs that still earn a nonzero efficiency bonus.
    Gangs that already earn $0 (gated below the entry level, or a fatal incident zeroed them
    out) cannot be reconstructed upward — there is no data signal to recover what they would
    have qualified for, so they are flagged "gated" and held at $0 regardless of lever changes.

    Not modelled here (see BONUS_POLICY_DEFAULTS comment for why): the §5.10/§5.11 Steep
    Stope m² uplift (real, active, but already baked into GANGTOTALSQMADJUSTED upstream of
    this data, not a separate Rand lever) and the §6.1.1/§6.1.2 Physical Conditions Rating
    safety forfeiture (searched PRODUCTIONEARN/GANGLINKEARN/RATES for any CONDITION/RATING/
    INSPECT column — none exists, so there's no data to drive it; every "clean" gang in a
    full year of data got exactly the full +25%, zero exceptions, so it either hasn't
    triggered yet or isn't wired into this data source).
    """
    _empty: dict = {"gangs": [], "summary": {}, "policy_defaults": BONUS_POLICY_DEFAULTS}
    df = _pull_stope_breaking_rows(period_from, period_to, section)
    if df.empty:
        return _empty

    # Two-way anchor to PARTICIPANTSDETAIL: Efficiency and Sweep are split back out of the
    # real STM total using the GANGPRODUCTIONDETAIL estimate's own efficiency:sweep ratio as
    # weights -- this preserves the formula's relative split (which only GANGPRODUCTIONDETAIL
    # can estimate, since it alone has the per-gang levers) while anchoring the absolute total
    # to real payroll. Driller and Safety take PARTICIPANTSDETAIL's own totals directly.
    # (The Bonus Analysis breakdown tables use _anchor_components() instead, which also splits
    # the safety share out of STM -- same grand total, finer split; see its docstring.)
    def _anchor(row):
        est_eff, est_sweep = float(row["efficiency_bonus"]), float(row["sweep_penalty"])
        est_stm, real_stm = est_eff + est_sweep, float(row["total_stm_bonus"])
        if abs(est_stm) > 1e-9:
            ratio = real_stm / est_stm
            return est_eff * ratio, est_sweep * ratio
        # No estimate signal to split by (e.g. a gated gang) -- if real payroll shows
        # something anyway, there's no basis to allocate it between efficiency/sweep,
        # so keep the whole real figure on efficiency rather than lose it from the total.
        return real_stm, 0.0

    anchored = df.apply(_anchor, axis=1, result_type="expand")
    df["efficiency_bonus"] = anchored[0]
    df["sweep_penalty"]    = anchored[1]
    df["drill_bonus"]      = df["total_driller_bonus"]
    df["safety_bonus"]     = df["total_safety_bonus"]

    defaults = BONUS_POLICY_DEFAULTS
    safety_defaults = defaults["safety_pct"]
    decomposed = df.apply(lambda r: _decompose_bonus_row(r, safety_defaults), axis=1, result_type="expand")
    df["raw_qualifying"], df["actual_safety_pct"], df["gated"] = (
        decomposed[0], decomposed[1], decomposed[2]
    )
    df["had_netting"] = df["netting_rate"] > 1.0001
    df["had_sw"]      = df["sw_factor"]    > 1.0001

    total_sqm_adj    = float(df["sqm"].sum())
    total_sqm_preadj = float(df["sqm_preadj"].sum())
    total_efficiency  = float(df["efficiency_bonus"].sum())
    total_drill  = float(df["drill_bonus"].sum())
    total_sweep_penalty  = float(df["sweep_penalty"].sum())
    total_safety = float(df["safety_bonus"].sum())
    gang_count   = len(df)
    gated_count  = int(df["gated"].sum())

    avg_efficiency_rate_adj    = round(total_efficiency / total_sqm_adj,    4) if total_sqm_adj    > 0 else 0.0
    avg_efficiency_rate_preadj = round(total_efficiency / total_sqm_preadj, 4) if total_sqm_preadj > 0 else 0.0

    gangs = [
        {
            "section":      row["section"],
            "period":       _period_label(int(row["period"])),
            "period_raw":   str(row["period"]),
            "gang":         row["gang"],
            "sqm_adj":      round(float(row["sqm"] or 0), 0),
            "sqm_preadj":   round(float(row["sqm_preadj"] or 0), 0),
            "labour":       round(float(row["labour"] or 0) if not pd.isna(row["labour"]) else 0, 2),
            "efficiency":   round(float(row["efficiency"] or 0), 2),
            "efficiency_bonus":  round(float(row["efficiency_bonus"] or 0), 2),
            "drill_bonus":  round(float(row["drill_bonus"] or 0), 2),
            "sweep_penalty":  round(float(row["sweep_penalty"] or 0), 2),
            "safety_bonus": round(float(row["safety_bonus"] or 0), 2),
            "raw_qualifying": round(float(row["raw_qualifying"] or 0), 2),
            "netting_rate": round(float(row["netting_rate"] or 1.0), 6),
            "sw_factor":    round(float(row["sw_factor"] or 1.0), 6),
            "had_netting":  bool(row["had_netting"]),
            "had_sw":       bool(row["had_sw"]),
            "had_wideraise": bool(row["had_wideraise"]),
            "gated":        bool(row["gated"]),
            "actual_safety_pct": round(float(row["actual_safety_pct"]), 2),
        }
        for _, row in df.iterrows()
    ]

    return {
        "gangs": gangs,
        "summary": {
            "total_efficiency":        round(total_efficiency, 2),
            "total_drill":        round(total_drill, 2),
            "total_sweep_penalty":        round(total_sweep_penalty, 2),
            "total_safety":       round(total_safety, 2),
            # total_bonus = efficiency + sweep only (the anchored STM reconstruction).
            # drill_bonus and safety_bonus are informational breakdowns, not additive —
            # verified against the source system's own EMPLOYEETOTALBONUS: a real employee's
            # total payout equals their STM figure alone, even when they hold a driller
            # role with a nonzero EMPLOYEEDRILLERBONUS, and EMPLOYEESAFETYBONUS is always
            # exactly 20% of STM (already counted inside it, not on top). See
            # _get_participants_bonus's docstring for the per-employee reconciliation.
            "total_bonus":        round(total_efficiency + total_sweep_penalty, 2),
            "total_sqm_adj":      round(total_sqm_adj, 0),
            "total_sqm_preadj":   round(total_sqm_preadj, 0),
            "gang_count":         gang_count,
            "gated_count":        gated_count,
            "avg_efficiency_rate_adj":    avg_efficiency_rate_adj,
            "avg_efficiency_rate_preadj": avg_efficiency_rate_preadj,
        },
        "policy_defaults": defaults,
    }


# ── Bonus Analysis breakdown tables (Mponeng/Pakisa pattern, Tshepong data) ──────────────────

def get_bonus_composition(period_from: int, period_to: int, section: str = "ALL") -> dict:
    """"What the Bonus Is Made Of" -- the paid STM Bonus built up step by step, Stope Breaking.

    Tshepong's STM (EMPLOYEESTOPETEAMBONUS, the real paid total) is NOT a flat sum of the
    schema's bonus columns: the efficiency part is MULTIPLICATIVE and the safety/sweep parts
    are ADDITIVE inside it (verified 541/541 gang-periods Jul-25..Apr-26, GANGPRODUCTIONBONUS
    = GANGFINALBREAKBONUS + GANGFINALSWEEPINGSBONUS + GANGFINALSAFETYBONUS, driller excluded):

        raw_qualifying -> x Netting (REV04 s5.13.4, 1.2) -> x B-Reef SW (s5.12, 1.1)
                       -> = Efficiency Bonus
        + Safety ladder (s6.1.3-6.1.6: clean +25% of Efficiency, dressing 0, LTI -25, fatal -100)
        + Sweeping Penalty (s5.14, signed, <= 0)
        = STM Bonus (paid)

    Each step's Rand is exact, not an approximation: the efficiency steps multiply in sequence
    onto the anchored efficiency figure, and anchored efficiency + safety + sweep = real STM
    by construction (_anchor_components). Gated gangs (R0 efficiency, no basis to reconstruct
    a raw base) carry their real figure through unchanged at every step, landing in Raw
    Qualifying rather than being misattributed to Netting/B-Reef.

    Reference rows (never added to the total): real PARTICIPANTSDETAIL Safety (cross-check
    against the anchored safety step), Driller (REV07 Quality Drilling -- tracked separately;
    the source system's EMPLOYEETOTALBONUS equals STM alone, so it is not part of STM) and
    AWOP (already netted inside STM).
    """
    df = _composition_frame(period_from, period_to, section)
    if df.empty:
        return {"waterfall": [], "reference": [], "by_section": [], "summary": {}}

    raw_total     = float(df["raw_step"].sum())
    netting_up    = float(df["netting_delta"].sum())
    sw_up         = float(df["sw_delta"].sum())
    eff_total     = float(df["efficiency_bonus"].sum())
    safety_total  = float(df["safety_bonus"].sum())
    sweep_total   = float(df["sweep_penalty"].sum())
    stm_total     = float(df["total_stm_bonus"].sum())
    real_safety   = float(df["total_safety_bonus"].sum())
    driller_total = float(df["total_driller_bonus"].sum())
    awop_total    = float(df["total_awop_bonus"].sum())

    waterfall = [
        {"step": "raw_qualifying", "label": "Raw Qualifying Bonus", "rand": round(raw_total, 2),
         "rule": "REV04 s5.4: efficiency-based (m2/empl) production bonus above the 14 m2/empl entry level "
                 "(Wide Raise exempt), looked up by team size -- before any uplift. Gated gangs' real figure, "
                 "where any, is carried here unchanged."},
        {"step": "netting", "label": "+ Netting Uplift", "rand": round(netting_up, 2),
         "rule": "s5.13.4: +20% of the Efficiency Bonus for workplaces with permanent netting installed "
                 "(real WORKPLACENETTINGRATE = 1.2). Applies to Efficiency only."},
        {"step": "breef", "label": "+ B-Reef Stope-Width Uplift", "rand": round(sw_up, 2),
         "rule": "s5.12: +10% where B-Reef stoping width is >= 1.60 m (real B_REEF_SW_FACTOR = 1.1)."},
        {"step": "efficiency", "label": "= Efficiency Bonus", "rand": round(eff_total, 2), "subtotal": True,
         "rule": "GANGFINALBREAKBONUS, anchored to real payroll. Raw Qualifying x Netting x B-Reef SW."},
        {"step": "safety", "label": "+/- Safety Ladder", "rand": round(safety_total, 2),
         "rule": "s6.1.3-6.1.6: clean record +25% of Efficiency, dressing-station failure 0%, LTI -25%, "
                 "fatality -100%. A separate component paid inside STM, not a multiplier on Efficiency."},
        {"step": "sweep", "label": "- Sweeping Penalty", "rand": round(sweep_total, 2),
         "rule": "s5.14: deduction for failing the sweepings standard (GANGFINALSWEEPINGSBONUS, signed)."},
        {"step": "total", "label": "= STM Bonus (paid)", "rand": round(stm_total, 2),
         "rule": "EMPLOYEESTOPETEAMBONUS -- the real paid total, the same figure as every Total Bonus on this "
                 "dashboard. Efficiency + Safety + Sweeping Penalty reconcile to it exactly."},
    ]
    reference = [
        {"key": "safety_real", "label": "Safety Bonus -- real payroll (reference)", "rand": round(real_safety, 2),
         "rule": "Real EMPLOYEESAFETYBONUS total. Already inside STM above (the Safety Ladder step is the same "
                 f"money, anchored); difference vs that step: {real_safety - safety_total:,.2f}."},
        {"key": "driller", "label": "Driller Bonus -- REV07 Quality Drilling (reference)", "rand": round(driller_total, 2),
         "rule": "Real EMPLOYEEDRILLERBONUS total -- a separate scheme tracked on its own. Not part of STM and "
                 "never added into any STM total on this dashboard (the source system's EMPLOYEETOTALBONUS "
                 "equals STM alone)."},
        {"key": "awop", "label": "AWOP Penalty (reference)", "rand": round(awop_total, 2),
         "rule": "Real EMPLOYEEAWOPPENALTY total -- already netted into STM above (raw payroll: "
                 "EMPLOYEETOTALBONUS equals STM, never STM + AWOP). Shown for visibility only."},
    ]

    def _comp_row(grp) -> dict:
        return {
            "raw_qualifying": round(float(grp["raw_step"].sum()), 2),
            "netting":        round(float(grp["netting_delta"].sum()), 2),
            "breef":          round(float(grp["sw_delta"].sum()), 2),
            "efficiency":     round(float(grp["efficiency_bonus"].sum()), 2),
            "safety":         round(float(grp["safety_bonus"].sum()), 2),
            "sweep":          round(float(grp["sweep_penalty"].sum()), 2),
            "total":          round(float(grp["total_stm_bonus"].sum()), 2),
            "gang_periods":   int(len(grp)),
        }

    by_section = [{"section": str(sec), **_comp_row(grp)} for sec, grp in df.groupby("section")]
    period_order = sorted(df["period"].unique())
    by_section_period = [
        {"section": str(sec), "period_raw": str(p), "period": _period_label(int(p)), **_comp_row(grp)}
        for (sec, p), grp in df.groupby(["section", "period"])
    ]

    return {
        "waterfall": waterfall,
        "reference": reference,
        "by_section": by_section,
        "by_section_period": by_section_period,
        "periods": [_period_label(int(p)) for p in period_order],
        "period_keys": [str(p) for p in period_order],
        "sections": sorted(df["section"].unique().tolist()),
        "summary": {
            "stm_total":        round(stm_total, 2),
            "efficiency_total": round(eff_total, 2),
            "gang_count":       int(len(df)),
            "gated_count":      int(df["gated"].sum()),
            "reconciles":       bool(abs((eff_total + safety_total + sweep_total) - stm_total) < 0.05),
        },
    }


def get_bonus_outcomes(period_from: int, period_to: int, section: str = "ALL") -> dict:
    """"Who Earned What -- Gang-Periods per Bonus Outcome", Stope Breaking.

    Classifies GANG-PERIODS (the finest grain every other table in this dashboard uses; the
    policy levers -- entry level, netting flag, B-Reef factor, incident indicators -- are all
    gang-level facts) by what each one earned on every lever. AWOP is the one personal signal
    (EMPLOYEEAWOPPENALTY, per employee) and is rolled up to "did this gang-period have any
    AWOP deduction" to stay on the same grain.

    Each outcome's Rand comes from the exact same per-row figures get_bonus_composition()
    sums, so the rows add back to that table by construction: the two Netting rows sum to its
    Netting Uplift; the two B-Reef rows to its B-Reef uplift; the four Safety rows to its
    Safety Ladder; the two Entry Level rows to its Efficiency Bonus; the AWOP "docked" row to
    its AWOP reference.
    """
    df = _composition_frame(period_from, period_to, section)
    if df.empty:
        return {"components": [], "total_gang_periods": 0}

    def _bucket(mask, rand_series):
        return int(mask.sum()), round(float(rand_series[mask].sum()), 2)

    gated = df["gated"]
    q_n,  q_rand  = _bucket(~gated, df["efficiency_bonus"])
    g_n,  g_rand  = _bucket(gated,  df["efficiency_bonus"])
    n_n,  n_rand  = _bucket(df["had_netting"],  df["netting_delta"])
    nn_n, nn_rand = _bucket(~df["had_netting"], df["netting_delta"])
    s_n,  s_rand  = _bucket(df["had_sw"],  df["sw_delta"])
    ns_n, ns_rand = _bucket(~df["had_sw"], df["sw_delta"])

    safety_rows = []
    for rung, label, rule in [
        ("clean",    "Clean -- +25%",            "s6.1.3: no reportable incident, full +25% safety bonus on Efficiency."),
        ("dressing", "Dressing station -- 0%",    "s6.1.4: dressing-station failure, safety bonus forfeited (0%)."),
        ("lti",      "LTI -- -25%",               "s6.1.5: lost-time injury, -25% safety penalty."),
        ("fatal",    "Fatal -- -100%",            "s6.1.6: fatality, -100% (bonus forfeited)."),
    ]:
        n, rand = _bucket(df["safety_rung"] == rung, df["safety_bonus"])
        safety_rows.append({"key": rung, "label": label, "rule": rule, "gang_periods": n, "rand": rand})

    awop_mask = df["total_awop_bonus"] != 0
    a_n,  a_rand  = _bucket(awop_mask,  df["total_awop_bonus"])
    na_n, na_rand = _bucket(~awop_mask, df["total_awop_bonus"])

    return {
        "total_gang_periods": int(len(df)),
        "components": [
            {"component": "entry", "title": "Entry Level (s5.4)", "rows": [
                {"key": "qualified", "label": "Earned Efficiency Bonus", "gang_periods": q_n, "rand": q_rand,
                 "rule": "Cleared the 14 m2/empl qualifying efficiency (or Wide Raise exempt) and earned a nonzero Efficiency Bonus."},
                {"key": "gated", "label": "No Efficiency Bonus (R0)", "gang_periods": g_n, "rand": g_rand,
                 "rule": "Real Efficiency Bonus of R0 -- below the entry level, or zeroed by a fatal incident."}]},
            {"component": "netting", "title": "Netting (s5.13.4)", "rows": [
                {"key": "netted", "label": "Netted -- +20%", "gang_periods": n_n, "rand": n_rand,
                 "rule": "Permanent netting installed (WORKPLACENETTINGRATE 1.2): +20% on Efficiency."},
                {"key": "not_netted", "label": "Not netted", "gang_periods": nn_n, "rand": nn_rand,
                 "rule": "No permanent netting -- no uplift."}]},
            {"component": "breef", "title": "B-Reef Stope-Width (s5.12)", "rows": [
                {"key": "uplifted", "label": "Uplifted -- +10%", "gang_periods": s_n, "rand": s_rand,
                 "rule": "B-Reef stoping width >= 1.60 m (B_REEF_SW_FACTOR 1.1): +10% on Efficiency."},
                {"key": "not_uplifted", "label": "Not uplifted", "gang_periods": ns_n, "rand": ns_rand,
                 "rule": "No B-Reef stope-width uplift applied."}]},
            {"component": "safety", "title": "Safety Ladder (s6.1)", "rows": safety_rows},
            {"component": "awop", "title": "AWOP", "rows": [
                {"key": "docked", "label": "AWOP deduction applied", "gang_periods": a_n, "rand": a_rand,
                 "rule": "At least one employee in the gang had an EMPLOYEEAWOPPENALTY this period. Already netted into STM -- visibility only."},
                {"key": "none", "label": "No AWOP", "gang_periods": na_n, "rand": na_rand,
                 "rule": "No AWOP deduction recorded for anyone in the gang."}]},
        ],
    }


def get_policy_discrepancies(period_from: int, period_to: int, section: str = "ALL") -> dict:
    """"Where the System Differs from the Policy" -- each written REV04 rule already examined in
    this project, checked live against the selected range. Reports only what has actually been
    verified; anything not checked record-by-record is listed in the UI as not yet checked
    rather than guessed at.
    """
    df = _composition_frame(period_from, period_to, section)
    if df.empty:
        return {"items": [], "gang_count": 0}
    n = int(len(df))

    netted = df[df["had_netting"]]
    netting_avg = round(float(netted["netting_rate"].mean()), 4) if len(netted) else None
    netting_exact = int((abs(netted["netting_rate"] - 1.2) < 1e-6).sum())

    sw = df[df["had_sw"]]
    sw_avg = round(float(sw["sw_factor"].mean()), 4) if len(sw) else None
    sw_exact = int((abs(sw["sw_factor"] - 1.1) < 1e-6).sum())

    clean = df[(df["safety_rung"] == "clean") & (df["efficiency_bonus"] > 0)]
    clean_n = int(len(clean))
    # Anchoring scales efficiency and safety by one common ratio per row, so the per-row
    # safety/efficiency ratio here IS the production formula's own (GANGFINALSAFETYBONUS /
    # GANGFINALBREAKBONUS) -- a real check of the calc engine against the written +25%.
    clean_row_ratio = (clean["safety_bonus"] / clean["efficiency_bonus"] * 100) if clean_n else None
    clean_at_25 = int((abs(clean_row_ratio - 25.0) < 0.01).sum()) if clean_n else 0
    real_vs_anchored_gap = round(float((df["total_safety_bonus"] - df["safety_bonus"]).sum()), 2)
    anchored_safety_total = float(df["safety_bonus"].sum())
    gap_pct = round(real_vs_anchored_gap / anchored_safety_total * 100, 2) if anchored_safety_total else None
    clean_real_stm_ratio = round(float(clean["total_safety_bonus"].sum() / clean["total_stm_bonus"].sum() * 100), 2) if clean_n and clean["total_stm_bonus"].sum() > 0 else None

    lti = df[df["safety_rung"] == "lti"]
    lti_n = int(len(lti))
    lti_ratios = (lti["safety_bonus"] / lti["efficiency_bonus"].where(lti["efficiency_bonus"] > 0)).dropna() * 100
    lti_at_25 = int((abs(lti_ratios + 25) < 0.5).sum())
    lti_at_50 = int((abs(lti_ratios + 50) < 0.5).sum())
    lti_rand  = round(float(lti["safety_bonus"].sum()), 2)

    fatal = df[df["safety_rung"] == "fatal"]
    fatal_n = int(len(fatal))
    fatal_stm = round(float(fatal["total_stm_bonus"].sum()), 2)
    fatal_real_safety = round(float(fatal["total_safety_bonus"].sum()), 2)

    sweep_rows = int((df["sweep_penalty"].abs() > 0.005).sum())
    sweep_total = round(float(df["sweep_penalty"].sum()), 2)

    below = df[df["efficiency"] < BONUS_POLICY_DEFAULTS["entry_threshold"]]
    below_n = int(len(below))
    below_paid = below[below["efficiency_bonus"] > 0]
    below_paid_n = int(len(below_paid))
    below_paid_wr = int(below_paid["had_wideraise"].sum())
    below_paid_rand = round(float(below_paid["efficiency_bonus"].sum()), 2)

    gated_n = int(df["gated"].sum())

    items = [
        {"key": "netting", "status": "matches" if netting_avg is not None and abs(netting_avg - 1.2) < 1e-6 else ("unverifiable" if netting_avg is None else "differs"),
         "title": "Netting Uplift -- matches the written 20% exactly",
         "policy": "s5.13.4: +20% of the Efficiency Bonus where permanent netting is installed (Efficiency only, not the total).",
         "system": f"Real WORKPLACENETTINGRATE is exactly 1.2 on {netting_exact} of {len(netted)} netted gang-periods (average {netting_avg}); 1.0 on the rest.",
         "rand": 0.0, "rand_label": "No discrepancy"},
        {"key": "breef", "status": "matches" if sw_avg is not None and abs(sw_avg - 1.1) < 1e-6 else ("unverifiable" if sw_avg is None else "differs"),
         "title": "B-Reef Stope-Width Uplift -- applied at the written 10%" if sw_avg is not None else "B-Reef Stope-Width Uplift -- not applied to any gang in this selection",
         "policy": "s5.12: +10% on the Efficiency Bonus where B-Reef stoping width is >= 1.60 m.",
         "system": (f"B_REEF_SW_FACTOR is exactly 1.1 on {sw_exact} of {len(sw)} uplifted gang-periods ({len(sw)} of {n} in this selection)."
                    if sw_avg is not None else f"0 of {n} gang-periods in this selection carry a B_REEF_SW_FACTOR above 1.0."),
         "rand": round(float(df["sw_delta"].sum()), 2), "rand_label": "Uplift paid in this selection"},
        {"key": "clean_safety",
         "status": ("unverifiable" if not clean_n else
                    "matches" if clean_at_25 == clean_n and gap_pct is not None and abs(gap_pct) < 2.0 else "differs"),
         "title": "Clean Safety Bonus -- the written +25% of Efficiency",
         "policy": "s6.1.3: a clean safety record earns a safety bonus of +25% of the Efficiency Bonus.",
         "system": (f"Calc engine: GANGFINALSAFETYBONUS is exactly 25% of GANGFINALBREAKBONUS on {clean_at_25} of {clean_n} clean gang-periods. "
                    f"Real payroll: EMPLOYEESAFETYBONUS is {clean_real_stm_ratio}% of real STM for the same gangs -- which is the same rule seen from "
                    f"the other side, because STM already contains the safety piece (0.25E / 1.25E = 20%). Real payroll safety sits "
                    f"{gap_pct}% away from the formula's share over all gangs."),
         "rand": real_vs_anchored_gap, "rand_label": "Real payroll safety minus the formula's safety share, all gangs"},
        {"key": "lti", "status": "differs" if lti_at_50 else ("matches" if lti_n else "unverifiable"),
         "title": "LTI penalty -- the system applied -50% on some gangs, the single written -25% on others",
         "policy": "s6.1.5 as modelled in the Simulator: one LTI rate of -25% of the Efficiency Bonus.",
         "system": (f"{lti_n} LTI gang-periods in this selection: {lti_at_25} at -25%, {lti_at_50} at -50% (production-formula ratio). "
                    "A second, steeper LTI tier is being applied by the calc engine that the Simulator's single -25% lever does not model."
                    if lti_n else "No LTI gang-periods in this selection."),
         "rand": lti_rand, "rand_label": "Total LTI safety penalty in this selection"},
        {"key": "fatal", "status": "differs" if fatal_n and fatal_real_safety != 0 else ("matches" if fatal_n else "unverifiable"),
         "title": "Fatal -- bonus forfeited, but a real safety deduction still posted",
         "policy": "s6.1.6: a fatality forfeits the bonus (-100%).",
         "system": (f"{fatal_n} fatal gang-period(s): real STM R{fatal_stm:,.2f} (forfeited as written) but real EMPLOYEESAFETYBONUS of R{fatal_real_safety:,.2f} "
                    "was still recorded against the gang -- a penalty posted on top of a zero bonus, which is not a percentage of anything."
                    if fatal_n else "No fatal gang-periods in this selection."),
         "rand": fatal_real_safety, "rand_label": "Real safety figure on fatal gang-periods"},
        {"key": "sweep", "status": "matches" if sweep_rows else "unverifiable",
         "title": "Sweeping Penalty -- active in the data",
         "policy": "s5.14: a deduction for failing the sweepings standard for the period.",
         "system": f"{sweep_rows} of {n} gang-periods carry a nonzero GANGFINALSWEEPINGSBONUS (always <= 0, i.e. a penalty, despite the column name).",
         "rand": sweep_total, "rand_label": "Total sweeping penalty in this selection (anchored)"},
        {"key": "entry", "status": "differs" if (below_paid_n - below_paid_wr) > 0 else ("matches" if below_n else "unverifiable"),
         "title": "Entry-Level Gate (14 m2/empl) -- gangs below the gate still earned an Efficiency Bonus",
         "policy": "s5.4: below 14 m2/empl no Efficiency Bonus is earned; Wide Raise panels are exempt.",
         "system": (f"{below_n} gang-periods are below 14 m2/empl on GANGEFFICIENCY; {below_paid_n} of them still earned a nonzero Efficiency Bonus, "
                    f"of which {below_paid_wr} are Wide Raise (exempt) and {below_paid_n - below_paid_wr} are not. {gated_n} gang-periods in total earned R0 efficiency. "
                    "Real payroll can carry pro-rata/acting amounts the production formula gates to zero, so this is a flag, not a proven breach."),
         "rand": below_paid_rand, "rand_label": "Efficiency Bonus paid to gangs below the gate"},
        {"key": "tier", "status": "matches",
         "title": "m2 Tier Escalator -- removed in REV04, and absent from the data",
         "policy": "REV02 s5.3 paid an extra 5-50% for 300m2+; REV04 has no such clause.",
         "system": "GANGFINALBREAKBONUS equals the pre-uplift efficiency estimate x netting x B-Reef SW regardless of m2 band -- no tier multiplier is applied.",
         "rand": 0.0, "rand_label": "No discrepancy"},
        {"key": "steep", "status": "matches",
         "title": "Steep Stope m2 uplift -- applied upstream, inside the m2 figure",
         "policy": "s5.10-5.11: dip 35-44 degrees adds +10% to a panel's m2, >= 45 degrees adds +20%.",
         "system": "PRODUCTIONWPDETAIL: WPTOTALM2 = WPPRETOTALM2 x DIP_FACTOR exactly, so it is already inside GANGTOTALSQMADJUSTED and every bonus derived from it.",
         "rand": None, "rand_label": "Not a Rand-side rule"},
        {"key": "conditions", "status": "unverifiable",
         "title": "Physical Conditions Rating safety forfeiture -- no data to check against",
         "policy": "s6.1.1-6.1.2: the safety bonus can be forfeited on a physical-conditions rating.",
         "system": "No CONDITION/RATING/INSPECT column exists in PRODUCTIONEARN, GANGLINKEARN or RATES; every clean gang in a full year earned the full +25% with zero exceptions.",
         "rand": None, "rand_label": "Not computable from available data"},
        {"key": "driller", "status": "unverifiable",
         "title": "Driller Bonus (REV07 Quality Drilling) -- tracked, but not part of the stope-team total",
         "policy": "REV07: a separate Quality Drilling incentive for the driller.",
         "system": (f"Real EMPLOYEEDRILLERBONUS totals R{float(df['total_driller_bonus'].sum()):,.2f} in this selection, yet the source system's EMPLOYEETOTALBONUS equals STM alone "
                    "even for driller-role employees. Whether and how it is paid sits outside this data, so it is shown as a reference figure only."),
         "rand": round(float(df["total_driller_bonus"].sum()), 2), "rand_label": "Driller figure recorded (not in STM)"},
    ]
    return {"items": items, "gang_count": n}


def get_crew_performance(period_from: int, period_to: int, section: str = "ALL") -> list[dict]:
    """Per-gang (crew) summary across the selected periods, for Gang Analysis -> Crew
    Performance. STOPE BREAKING only, same scoping/anchoring as get_section_ranking() /
    _get_participants_bonus() -- real PARTICIPANTSDETAIL bonus, not the GANGPRODUCTIONDETAIL
    per-person-rate estimate. m2/Man = total adj m2 / total GANGLABOUR over the gang's months.
    """
    sf = _section_filter(section)
    sqm_df = read_sql(f"""
        SELECT section, period, gang, MAX(sqm) AS sqm, MAX(labour) AS labour
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm,
                TRY_CAST(GANGLABOUR           AS FLOAT) AS labour
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              {sf}
        ) AS q
        GROUP BY section, period, gang
    """)
    if sqm_df.empty:
        return []
    sqm_df["sqm"]    = sqm_df["sqm"].fillna(0).astype(float)
    sqm_df["labour"] = sqm_df["labour"].fillna(0).astype(float)
    sqm_df["period"] = sqm_df["period"].astype(str)

    bonus_df = _get_participants_bonus(period_from, period_to, section)
    if not bonus_df.empty:
        keys = sqm_df[["section", "period", "gang"]].drop_duplicates()
        bonus_sb = keys.merge(bonus_df[["section", "period", "gang", "total_bonus"]],
                              on=["section", "period", "gang"], how="left")
        bonus_sb["total_bonus"] = bonus_sb["total_bonus"].fillna(0).astype(float)
    else:
        bonus_sb = sqm_df[["section", "period", "gang"]].copy()
        bonus_sb["total_bonus"] = 0.0
    sqm_df = sqm_df.merge(bonus_sb, on=["section", "period", "gang"], how="left")
    sqm_df["total_bonus"] = sqm_df["total_bonus"].fillna(0.0)

    agg = sqm_df.groupby(["section", "gang"]).agg(
        total_sqm    = ("sqm",         "sum"),
        total_bonus  = ("total_bonus", "sum"),
        total_labour = ("labour",      "sum"),
        months       = ("period",      "nunique"),
    ).reset_index()
    agg["sqm_per_man"] = agg.apply(lambda r: r["total_sqm"] / r["total_labour"] if r["total_labour"] > 0 else 0.0, axis=1)
    agg["r_per_sqm"]   = agg.apply(lambda r: r["total_bonus"] / r["total_sqm"] if r["total_sqm"] > 0 else 0.0, axis=1)
    agg = agg.sort_values("total_sqm", ascending=False).reset_index(drop=True)
    return [
        {"section": str(r["section"]), "gang": str(r["gang"]),
         "total_sqm": round(float(r["total_sqm"]), 0), "total_bonus": round(float(r["total_bonus"]), 2),
         "months": int(r["months"]), "sqm_per_man": round(float(r["sqm_per_man"]), 2),
         "r_per_sqm": round(float(r["r_per_sqm"]), 2)}
        for _, r in agg.iterrows()
    ]


# ── Labour Analysis: Crew Labour / Drill Down / Best vs Worst (Mponeng pattern, Tshepong data) ──

# Sick-leave day codes in the attendance register (BONUSSHIFTS, one code per day DAY0..DAY44).
# Codes are case-sensitive (p = sick, P = Sunday off). From the EVENTCODE table: C sick note Harm
# Drs, S sick (hospital), c sick note private Dr, f / F TB cases, m / M mine accident sick /
# hospital, p sick (OHC PD 449), s sick reporting, 2 sick leave covid policy, 7 sick due to side
# effect of covid.
SICK_CODES = "'C', 'S', 'c', 'f', 'F', 'm', 'M', 'p', 's', '2', '7'"
CREW_GANGTYPES = ("STOPE BREAKING", "STOPE CLEANING")


def _crew_labour_period(p: int) -> dict:
    """Raw per-month ingredients for the Crew Labour tab, from PARTICIPANTSEARN<p> (the bonus
    calculation's participant rows, officials excluded) and the BONUSSHIFTS attendance register.

    Crew = the breaking gang's code (GANGPRODUCTIONDETAIL.CREWNO equals the STOPE BREAKING gang
    code on 541/541 Jul-25..Apr-26 crew-months; every breaking and cleaning gang belongs to
    exactly one crew). PARTICIPANTSEARN has no crew column, so people are mapped to a crew
    through their gang. Centre Gully / Surface Assistant gangs are shared between crews (165 of
    their gang-months map to 2+ crews) so they are not part of crew labour.

    Payroll supplies shifts worked, efficiency shifts, AWOP shifts and the bonus; the register
    supplies sick days only (its own SHIFTS_WORKED / AWOP_SHIFTS differ from payroll's on 20 and
    141 of 1,519 matched rows in Apr-26, so payroll stays the single source for those). Register
    rows join on (section, employee, gang, wage code), unique on both sides.
    """
    types = ", ".join(f"'{t}'" for t in CREW_GANGTYPES)
    tbl = f"PARTICIPANTSEARN{p}"
    out = {"rows": [], "persons": [], "gang": [], "sick": {}}
    base = f"""FROM [{tbl}]
               WHERE RTRIM(EMPLOYEETYPE) <> '1 - OFFICIALS' AND UPPER(RTRIM(GANGTYPE)) IN ({types})"""
    out["rows"] = read_sql(f"""
        SELECT RTRIM(SECTION) AS section, RTRIM(GANG) AS gang, UPPER(RTRIM(GANGTYPE)) AS gang_type,
               RTRIM(WAGECODE) AS wagecode, MAX(RTRIM(WAGE_DESCRIPTION)) AS descr,
               COUNT(DISTINCT EMPLOYEE_NO) AS employees,
               SUM(ISNULL(TRY_CAST(SAFETYREPIND AS FLOAT), 0)) AS reps,
               SUM(ISNULL(TRY_CAST(SHIFTS_WORKED AS FLOAT), 0)) AS shifts,
               SUM(ISNULL(TRY_CAST(EFF_SHIFTS AS FLOAT), 0)) AS eff,
               SUM(ISNULL(TRY_CAST(AWOP_SHIFTS AS FLOAT), 0)) AS awop_shifts,
               MAX(TRY_CAST(MONTHSHIFTS AS FLOAT)) AS ms,
               SUM(ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0)) AS stm,
               SUM(ISNULL(TRY_CAST(EMPLOYEESAFETYBONUS AS FLOAT), 0)) AS safety,
               SUM(ISNULL(TRY_CAST(EMPLOYEEAWOPPENALTY AS FLOAT), 0)) AS awop_pen
        {base}
        GROUP BY RTRIM(SECTION), RTRIM(GANG), UPPER(RTRIM(GANGTYPE)), RTRIM(WAGECODE)
    """).to_dict("records")
    out["persons"] = read_sql(f"""
        SELECT RTRIM(SECTION) AS section, RTRIM(GANG) AS gang, RTRIM(EMPLOYEE_NO) AS emp,
               MAX(CASE WHEN TRY_CAST(SAFETYREPIND AS FLOAT) > 0 THEN 1 ELSE 0 END) AS rep
        {base}
        GROUP BY RTRIM(SECTION), RTRIM(GANG), RTRIM(EMPLOYEE_NO)
    """).to_dict("records")
    # Gang shifts and month shifts, for the labour as the bonus system counts it.
    out["gang"] = read_sql(f"""
        SELECT RTRIM(GANG) AS gang, MAX(TRY_CAST(GANGSHIFTS AS FLOAT)) AS gs,
               MAX(TRY_CAST(MONTHSHIFTS AS FLOAT)) AS ms
        FROM [{tbl}] WHERE UPPER(RTRIM(GANGTYPE)) IN ({types}) GROUP BY RTRIM(GANG)
    """).to_dict("records")
    day = " + ".join(f"CASE WHEN RTRIM(b.DAY{i}) COLLATE Latin1_General_CS_AS IN ({SICK_CODES}) THEN 1 ELSE 0 END"
                     for i in range(45))
    sick = read_sql(f"""
        SELECT RTRIM(b.SECTION) AS section, RTRIM(b.GANG) AS gang, RTRIM(b.WAGECODE) AS wagecode, SUM({day}) AS sick
        FROM BONUSSHIFTS b
        JOIN (SELECT DISTINCT RTRIM(SECTION) AS section, RTRIM(EMPLOYEE_NO) AS emp, RTRIM(GANG) AS gang,
                     RTRIM(WAGECODE) AS wagecode
              {base}) x
          ON x.emp = RTRIM(b.EMPLOYEE_NO) AND x.gang = RTRIM(b.GANG) AND x.section = RTRIM(b.SECTION)
         AND x.wagecode = RTRIM(b.WAGECODE)
        WHERE RTRIM(b.PERIOD) = '{p}'
        GROUP BY RTRIM(b.SECTION), RTRIM(b.GANG), RTRIM(b.WAGECODE)
    """)
    out["sick"] = {(r["gang"], r["wagecode"]): float(r["sick"] or 0) for r in sick.to_dict("records")}
    return out


def get_crew_labour(period_from: int, period_to: int, section: str = "ALL") -> dict:
    """Labour Analysis -> Crew Labour: one row per crew x gang x wage code x month ("people"),
    plus per crew-month m2, labour and LTI ("crews"). The page adds them up for the chosen
    section / month / crew.

    Per crew-month (GANGPRODUCTIONDETAIL / PARTICIPANTSEARN, Stope Breaking + Cleaning gangs):
      lab_t = GANGLABOUR, the crew's labour as the bonus system counts it -- verified on 541/541
              crew-months to be CEIL((breaking GANGSHIFTS + cleaning GANGSHIFTS) / MONTHSHIFTS),
              and GANGEFFICIENCY = m2 / GANGLABOUR exactly on 541/541;
      lab_b / lab_c = the breaking / cleaning gang's own shifts / MONTHSHIFTS (unrounded; the
              crew total is the ceiling of their sum, so it can exceed it by less than 1);
      m2    = the breaking gang's adjusted m2 (the crew's m2);
      lti   = 1 when any of the crew's breaking / cleaning gangs had GANGLTIIND > 0.
    Bonus: prod = STM minus its safety part, safety = EMPLOYEESAFETYBONUS, so prod + safety = the
    paid STM exactly (STM already contains the safety share -- see get_bonus_composition).
    The Gang Types filter does not apply here: crew labour is defined for the breaking and
    cleaning gangs together.
    """
    sf = _section_filter(section)
    gpd = read_sql(f"""
        SELECT LTRIM(RTRIM(SECTION)) AS section, LTRIM(RTRIM(PERIOD)) AS period, LTRIM(RTRIM(GANG)) AS gang,
               UPPER(LTRIM(RTRIM(GANGTYPE))) AS gang_type, LTRIM(RTRIM(CREWNO)) AS crew,
               MAX(TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT)) AS m2, MAX(TRY_CAST(GANGLABOUR AS FLOAT)) AS lab,
               MAX(TRY_CAST(GANGLTIIND AS FLOAT)) AS lti
        FROM [GANGPRODUCTIONDETAIL]
        WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) IN ('STOPE BREAKING', 'STOPE CLEANING')
          AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
          AND LEN(LTRIM(RTRIM(CREWNO))) >= 8 {sf}
        GROUP BY LTRIM(RTRIM(SECTION)), LTRIM(RTRIM(PERIOD)), LTRIM(RTRIM(GANG)),
                 UPPER(LTRIM(RTRIM(GANGTYPE))), LTRIM(RTRIM(CREWNO))
    """)
    periods = sorted({p["value"] for p in get_available_periods() if period_from <= p["value"] <= period_to})
    exists = read_sql("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME LIKE 'PARTICIPANTSEARN2%'")
    have = set(exists["TABLE_NAME"].str.upper()) if not exists.empty else set()
    periods = [p for p in periods if f"PARTICIPANTSEARN{p}".upper() in have]
    if gpd.empty or not periods:
        return {"people": [], "crews": [], "period_labels": {}}
    gpd["period"] = gpd["period"].astype(str)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as ex:      # one set of SQL queries per month: run them side by side
        labs = dict(zip(periods, ex.map(_crew_labour_period, periods)))

    people, crews = [], []
    for p in periods:
        lab = labs[p]
        month = gpd[gpd["period"] == str(p)]
        if month.empty:
            continue
        crew_of = {r["gang"]: (r["section"], r["crew"]) for r in month.to_dict("records")}
        gang_sh = {r["gang"]: r for r in lab["gang"]}
        for r in lab["rows"]:
            hit = crew_of.get(r["gang"])
            if not hit or hit[0] != r["section"]:
                continue
            stm, safety = float(r["stm"] or 0), float(r["safety"] or 0)
            people.append({
                "period": str(p), "section": r["section"], "crew": hit[1], "gang": r["gang"], "gang_type": r["gang_type"],
                "wagecode": r["wagecode"], "descr": r["descr"], "employees": int(r["employees"] or 0),
                "reps": float(r["reps"] or 0), "shifts": float(r["shifts"] or 0), "eff": float(r["eff"] or 0),
                "awop_shifts": float(r["awop_shifts"] or 0), "ms": float(r["ms"] or 0),
                "prod": round(stm - safety, 2), "safety": round(safety, 2), "awop_pen": round(float(r["awop_pen"] or 0), 2),
                "sick": lab["sick"].get((r["gang"], r["wagecode"]), 0.0),
            })
        per = {}
        for g in month.to_dict("records"):
            c = per.setdefault((g["section"], g["crew"]), {"period": str(p), "section": g["section"], "crew": g["crew"],
                                                           "m2": 0.0, "lab_t": 0.0, "lab_b": 0.0, "lab_c": 0.0,
                                                           "lti": 0, "reps": []})
            c["lab_t"] = max(c["lab_t"], g["lab"] or 0)
            sh = gang_sh.get(g["gang"])
            ratio = (sh["gs"] / sh["ms"]) if sh and sh["ms"] and sh["ms"] > 0 and sh["gs"] is not None else 0.0
            if g["gang_type"] == "STOPE BREAKING":
                c["m2"] = max(c["m2"], g["m2"] or 0)
                c["lab_b"] += ratio
            else:
                c["lab_c"] += ratio
            if (g["lti"] or 0) > 0:
                c["lti"] = 1
        for x in lab["persons"]:
            hit = crew_of.get(x["gang"])
            if hit and hit[0] == x["section"] and x["rep"]:
                per[hit]["reps"].append(x["emp"])
        for c in per.values():
            c["reps"] = sorted(set(c["reps"]))
            c["lab_b"], c["lab_c"] = round(c["lab_b"], 4), round(c["lab_c"], 4)
            crews.append(c)
    return {"people": people, "crews": crews, "period_labels": {str(p): _period_label(int(p)) for p in periods}}
