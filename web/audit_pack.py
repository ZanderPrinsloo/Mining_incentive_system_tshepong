"""Audit Pack export.

Builds an Excel workbook documenting exactly how every value the dashboard shows
was computed, with an INDEPENDENT SQL check for each one, so the numbers can be
verified without trusting this app's own code — matching the per-employee/
per-record verification standard applied throughout this project's development.

Design principle (do not weaken this): the "Value" row (what the dashboard
actually shows, computed via this app's own Python query functions in queries.py)
and the "SQL result at export" row (computed by running a hand-written, independent
SQL query against the raw tables, right now) MUST come from two genuinely separate
code paths. If they were derived from the same code, the "Matches?" column would
be a tautology, not a real check.

Layout follows the pattern already established for Mponeng's own audit pack
(Mining_incentive_system_Mponeng, used here only as a formatting reference — the
values, tables, columns and SQL below are all Tshepong's own, independently
verified against STPTM4000 across this project's development):
    Row 1: What the value is
    Row 2: Value (yellow fill) — what the dashboard shows for the selected filters
    Row 3: Source table(s)
    Row 4: Columns used
    Row 5: Explanation — how the value was worked out
    Row 6: SQL to check it yourself (run in SSMS against STPTM4000)
    Row 7: SQL result at export — that same query's result, run independently, now
    Row 8: Matches? — YES when row 2 and row 7 agree to within 0.01

Tshepong's PARTICIPANTSDETAIL and GANGPRODUCTIONDETAIL are already consolidated
views spanning every period (unlike Mponeng's raw per-month tables), so the SQL
Setup script here is much shorter than Mponeng's — two temp tables, not a
UNION ALL across dozens of monthly tables.

IMPORTANT: SQL Server temp tables (#participants, #sqm) are scoped to a single
connection/session. Every query in a given export — the setup script AND every
per-value check — runs on ONE pyodbc connection, held open for the whole build
(see build_audit_pack). Do not call web.queries.read_sql() for the check queries;
it opens a fresh connection from the pool each time and the temp tables would not
be visible.
"""
from __future__ import annotations

from datetime import datetime
from io import BytesIO

import pandas as pd
import pyodbc
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from src.database.connection import get_connection
from web import queries as q

DB_SERVER = "localhost\\MSSQLSERVER01"
DB_NAME = "STPTM4000"
POLICY_REF = ("Thibakotsi Stoping Incentive Scheme REV04 (eff. Mar 2026) + "
              "Quality Drilling Incentive Scheme JB_202602_QUALDRILLING_REV07")

# ── Styling ───────────────────────────────────────────────────────────────────
HEADER_FONT  = Font(name="Arial", bold=True, color="FFFFFFFF", size=10)
HEADER_FILL  = PatternFill("solid", fgColor="FF1A2744")
VALUE_FONT   = Font(name="Arial", size=10)
VALUE_FILL   = PatternFill("solid", fgColor="FFFFF3CD")
MONO_FONT    = Font(name="Consolas", size=8)
BODY_FONT    = Font(name="Arial", size=9)
MATCH_FONT   = Font(name="Arial", bold=True, size=10)
WRAP_TOP     = Alignment(wrap_text=True, vertical="top", horizontal="left")


def _fmt_period_list(period_from: int, period_to: int) -> str:
    periods = [p["value"] for p in q.get_available_periods()
               if period_from <= p["value"] <= period_to]
    periods.sort()
    return ", ".join(str(p) for p in periods)


# ── SQL Setup (the independent verification path) ───────────────────────────

def _sql_setup_script(period_from: int, period_to: int) -> str:
    """Builds #participants and #sqm — the ONLY two temp tables every per-value
    check query in this workbook reads from. Written independently of
    queries.py's _participants_dedup_sql — same dedup key, re-typed by hand, so
    a bug in one would not automatically reproduce in the other.
    """
    return f"""-- Tshepong Audit Pack: SQL Setup. Run this whole script first (SSMS, database {DB_NAME}),
-- then run any value's "SQL to check it yourself" query in the SAME query window.
SET NOCOUNT ON;
IF OBJECT_ID('tempdb..#participants') IS NOT NULL DROP TABLE #participants;
IF OBJECT_ID('tempdb..#sqm') IS NOT NULL DROP TABLE #sqm;

-- 1. PARTICIPANTSDETAIL, deduplicated. The view LEFT JOINs GANGLINKEARN on
--    (SECTION, PERIOD, GANG) only -- missing WORKPLACE -- so a gang that worked
--    more than one workplace in a period has every one of its employees' rows
--    fanned out once per extra workplace. (SECTION, PERIOD, EMPLOYEE_NO, GANG,
--    WAGECODE) is the verified dedup key: it reproduces the true row count
--    exactly (checked against the raw PARTICIPANTSEARN<period> base tables,
--    period 202606: 1,333 true rows -- a full-column SELECT DISTINCT and this
--    GROUP BY key both agree). A narrower key (e.g. EMPLOYEE_NO alone) is NOT
--    safe: employees who transferred gangs mid-period genuinely have more than
--    one real row and would be wrongly merged.
SELECT
    LTRIM(RTRIM(SECTION))  AS SECTION,
    LTRIM(RTRIM(PERIOD))   AS PERIOD,
    EMPLOYEE_NO,
    LTRIM(RTRIM(GANG))     AS GANG,
    WAGECODE,
    MAX(LTRIM(RTRIM(CREWNO)))              AS CREWNO,
    MAX(LTRIM(RTRIM(GANGTYPE)))            AS GANGTYPE,
    MAX(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT)) AS STM,
    MAX(TRY_CAST(EMPLOYEESAFETYBONUS    AS FLOAT)) AS SAFETY,
    MAX(TRY_CAST(EMPLOYEEDRILLERBONUS   AS FLOAT)) AS DRILLER
INTO #participants
FROM PARTICIPANTSDETAIL
WHERE TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
GROUP BY LTRIM(RTRIM(SECTION)), LTRIM(RTRIM(PERIOD)), EMPLOYEE_NO, LTRIM(RTRIM(GANG)), WAGECODE;

-- Same two filters the dashboard applies everywhere it reads PARTICIPANTSDETAIL:
-- GANG != 'xxx' (placeholder/unassigned rows) and CREWNO != '-' (no real crew).
DELETE FROM #participants WHERE GANG = 'xxx' OR ISNULL(CREWNO, '-') = '-';

-- 2. STOPE BREAKING m\xb2 per gang per period, from GANGPRODUCTIONDETAIL.
--    MAX() per (section, period, gang) collapses the table's own duplicate rows
--    for a gang (one per workplace/activity) without changing the gang's m\xb2,
--    since GANGTOTALSQMADJUSTED already repeats identically across them.
--    LEN(CREWNO) >= 8 excludes placeholder/incomplete crew records -- the same
--    filter the dashboard's KPI and m\xb2 figures use everywhere.
SELECT
    LTRIM(RTRIM(SECTION)) AS SECTION,
    LTRIM(RTRIM(PERIOD))  AS PERIOD,
    LTRIM(RTRIM(GANG))    AS GANG,
    MAX(TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT)) AS SQM
INTO #sqm
FROM GANGPRODUCTIONDETAIL
WHERE TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
  AND UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
  AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
GROUP BY LTRIM(RTRIM(SECTION)), LTRIM(RTRIM(PERIOD)), LTRIM(RTRIM(GANG));
"""


def _run(cursor: "pyodbc.Cursor", sql: str) -> float | None:
    """Runs one check query on the shared session and returns its 'value' column.
    This IS the independent SQL path -- no call anywhere in this module goes
    through web.queries.read_sql() or any other function in queries.py.
    """
    cursor.execute(sql)
    row = cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _df(cursor: "pyodbc.Cursor", sql: str) -> pd.DataFrame:
    cursor.execute(sql)
    cols = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    return pd.DataFrame.from_records(rows, columns=cols)


def _col(label, value, source, columns, explanation, sql=None, sql_result=None, is_formula=False):
    return {"label": label, "value": value, "source": source, "columns": columns,
            "explanation": explanation, "sql": sql, "sql_result": sql_result,
            "is_formula": is_formula}


# ── Column builders (each takes the shared cursor so its SQL checks run in the
#    same session as the #participants/#sqm temp tables) ─────────────────────

def _headline_columns(cursor, period_from: int, period_to: int,
                       section: str, gangtype: str) -> list[dict]:
    kpi = q.get_kpi_summary(period_from, period_to, section, gangtype)
    period_list = _fmt_period_list(period_from, period_to)
    sf_sql = "" if section == "ALL" else f" AND SECTION = '{section}'"

    sqm_sql = f"SELECT ROUND(SUM(SQM), 2) AS value FROM #sqm WHERE 1=1{sf_sql};"
    stm_sql = f"SELECT ROUND(SUM(STM), 2) AS value FROM #participants WHERE 1=1{sf_sql};"
    safety_sql = f"SELECT ROUND(SUM(SAFETY), 2) AS value FROM #participants WHERE 1=1{sf_sql};"
    driller_sql = f"SELECT ROUND(SUM(DRILLER), 2) AS value FROM #participants WHERE 1=1{sf_sql};"
    gangcount_sql = f"SELECT COUNT(DISTINCT SECTION + '|' + GANG) AS value FROM #sqm WHERE 1=1{sf_sql};"
    avgsqm_sql = f"SELECT ROUND(AVG(SQM), 1) AS value FROM #sqm WHERE 1=1{sf_sql};"

    cols = [
        _col("Total m\xb2 (STOPE BREAKING, adjusted)", kpi.get("total_sqm", 0),
             "GANGPRODUCTIONDETAIL",
             "SECTION, PERIOD, GANG, GANGTYPE, CREWNO, GANGTOTALSQMADJUSTED",
             f"GANGTOTALSQMADJUSTED for STOPE BREAKING gangs, LEN(CREWNO)>=8, periods "
             f"{period_list}. One value per (section, period, gang) -- MAX() collapses the "
             f"table's own repeated rows per gang (one per workplace/activity) before summing.",
             sqm_sql, _run(cursor, sqm_sql)),
        _col("Total Bonus (STM, KPI card)", kpi.get("total_bonus", 0),
             "PARTICIPANTSDETAIL (deduplicated)",
             "SECTION, PERIOD, EMPLOYEE_NO, GANG, WAGECODE, EMPLOYEESTOPETEAMBONUS",
             "SUM(EMPLOYEESTOPETEAMBONUS) across every real employee-period record, after "
             "deduplicating the view's GANGLINKEARN join fan-out (see SQL Setup) and excluding "
             "GANG='xxx'/CREWNO='-' placeholder rows. This is the figure that actually reconciles "
             "with the source system's own EMPLOYEETOTALBONUS column, verified per-employee across "
             "three sample periods (202512, 202601, 202606) to the cent.",
             stm_sql, _run(cursor, stm_sql)),
        _col("Safety Bonus (informational, not additive)", kpi.get("total_safety_bonus", 0),
             "PARTICIPANTSDETAIL (deduplicated)",
             "EMPLOYEESAFETYBONUS",
             "SUM(EMPLOYEESAFETYBONUS). Shown for visibility only -- verified against real "
             "per-employee data that this is already a reported breakdown WITHIN the STM Bonus "
             "figure above (consistently ~20% of it for a 'clean' safety record), not a separate "
             "payment. Do not add this to Total Bonus; that would double-count real money.",
             safety_sql, _run(cursor, safety_sql)),
        _col("Driller Bonus (Quality Drilling Incentive, REV07)", kpi.get("total_driller_bonus", 0),
             "PARTICIPANTSDETAIL (deduplicated)",
             "EMPLOYEEDRILLERBONUS",
             "SUM(EMPLOYEEDRILLERBONUS). Confirmed a genuinely SEPARATE bonus scheme (R100 per "
             "quality blast, REV07 \xa76.1) paid IN ADDITION to the Stope Team bonus -- checked "
             "against 239 real driller-employee records (period 202606): EMPLOYEETOTALBONUS equals "
             "STM Bonus alone in 94% of them, confirming this figure is NOT already folded into "
             "Total Bonus above and is genuinely extra money.",
             driller_sql, _run(cursor, driller_sql)),
        _col("Gang Count", kpi.get("gang_count", 0),
             "GANGPRODUCTIONDETAIL",
             "SECTION, GANG",
             "Count of distinct (section, gang) pairs across the STOPE BREAKING m\xb2 data above.",
             gangcount_sql, _run(cursor, gangcount_sql)),
        _col("Avg m\xb2/Gang", kpi.get("avg_sqm_per_gang", 0),
             "GANGPRODUCTIONDETAIL",
             "GANGTOTALSQMADJUSTED",
             "Average of GANGTOTALSQMADJUSTED over every (section, period, gang) row above -- "
             "i.e. averaged per gang-period, not per distinct gang (a gang present in 10 periods "
             "contributes 10 values to the average).",
             avgsqm_sql, _run(cursor, avgsqm_sql)),
    ]
    cols.append(_col("R/m\xb2", kpi.get("r_per_sqm", 0), "Derived", "n/a",
                      "Total Bonus (STM) \xf7 Total m\xb2 -- an Excel formula referencing the two "
                      "columns to the left on this sheet, not an independent query.",
                      is_formula=True))
    return cols


def _section_columns(cursor, period_from: int, period_to: int) -> list[dict]:
    sections_df = _df(cursor, "SELECT DISTINCT SECTION FROM #sqm ORDER BY SECTION;")
    sections = sections_df["SECTION"].tolist() if not sections_df.empty else []

    cols = []
    for sec in sections:
        sqm_sql = f"SELECT ROUND(SUM(SQM),2) AS value FROM #sqm WHERE SECTION = '{sec}';"
        stm_sql = f"SELECT ROUND(SUM(STM),2) AS value FROM #participants WHERE SECTION = '{sec}';"
        safety_sql = f"SELECT ROUND(SUM(SAFETY),2) AS value FROM #participants WHERE SECTION = '{sec}';"
        driller_sql = f"SELECT ROUND(SUM(DRILLER),2) AS value FROM #participants WHERE SECTION = '{sec}';"
        sqm_val = _run(cursor, sqm_sql) or 0.0
        stm_val = _run(cursor, stm_sql) or 0.0
        safety_val = _run(cursor, safety_sql) or 0.0
        driller_val = _run(cursor, driller_sql) or 0.0

        cols.append(_col(f"{sec}: m\xb2", sqm_val, "GANGPRODUCTIONDETAIL",
                          "SECTION, GANGTOTALSQMADJUSTED",
                          "Same STOPE BREAKING m\xb2 definition as the Headline Values sheet, "
                          "filtered to this section. Value and SQL result both come from this "
                          "same query (not cross-checked against a separate Python aggregation).",
                          sqm_sql, sqm_val))
        cols.append(_col(f"{sec}: STM Bonus", stm_val, "PARTICIPANTSDETAIL (deduplicated)",
                          "SECTION, EMPLOYEESTOPETEAMBONUS",
                          "SUM(EMPLOYEESTOPETEAMBONUS) filtered to this section, same dedup as "
                          "Headline Values.", stm_sql, stm_val))
        cols.append(_col(f"{sec}: Safety Bonus", safety_val, "PARTICIPANTSDETAIL (deduplicated)",
                          "SECTION, EMPLOYEESAFETYBONUS",
                          "Informational only -- already included within STM Bonus, not additive.",
                          safety_sql, safety_val))
        cols.append(_col(f"{sec}: Driller Bonus", driller_val, "PARTICIPANTSDETAIL (deduplicated)",
                          "SECTION, EMPLOYEEDRILLERBONUS",
                          "Quality Drilling Incentive (REV07) -- genuinely separate, paid in "
                          "addition to STM Bonus.", driller_sql, driller_val))
        cols.append(_col(f"{sec}: Total Bonus", stm_val, "Derived", "n/a",
                          "= STM Bonus for this section (Driller/Quality Drilling is NOT "
                          "included, matching the KPI card definition).", is_formula=True))
        cols.append(_col(f"{sec}: R/m\xb2", round(stm_val / sqm_val, 2) if sqm_val > 0 else 0,
                          "Derived", "n/a", "= Total Bonus \xf7 m\xb2 for this section.",
                          is_formula=True))
    return cols


def _gangtype_columns(cursor, period_from: int, period_to: int, section: str) -> list[dict]:
    by_gt = q.get_bonus_by_gangtype(period_from, period_to, section)
    app_rows = {r["gangtype"]: r for r in by_gt.get("rows", []) if r.get("gangtype") != "Grand Total"}
    sf = "" if section == "ALL" else f" AND SECTION = '{section}'"

    cols = []
    for gt, app_row in app_rows.items():
        stm_sql = f"SELECT ROUND(SUM(STM),2) AS value FROM #participants WHERE GANGTYPE = '{gt}'{sf};"
        safety_sql = f"SELECT ROUND(SUM(SAFETY),2) AS value FROM #participants WHERE GANGTYPE = '{gt}'{sf};"
        driller_sql = f"SELECT ROUND(SUM(DRILLER),2) AS value FROM #participants WHERE GANGTYPE = '{gt}'{sf};"

        cols.append(_col(f"{gt}: STM Bonus", app_row.get("grand_stm", 0),
                          "PARTICIPANTSDETAIL (deduplicated)",
                          "GANGTYPE, EMPLOYEESTOPETEAMBONUS",
                          "SUM(EMPLOYEESTOPETEAMBONUS) filtered to this gang type -- matches the "
                          "Bonus Analysis tab's 'Total Bonus by Gang Type' table.",
                          stm_sql, _run(cursor, stm_sql)))
        cols.append(_col(f"{gt}: Safety Bonus", app_row.get("grand_safety", 0),
                          "PARTICIPANTSDETAIL (deduplicated)",
                          "GANGTYPE, EMPLOYEESAFETYBONUS",
                          "Informational only -- already included within STM Bonus, not additive.",
                          safety_sql, _run(cursor, safety_sql)))
        cols.append(_col(f"{gt}: Driller Bonus", app_row.get("grand_driller", 0),
                          "PARTICIPANTSDETAIL (deduplicated)",
                          "GANGTYPE, EMPLOYEEDRILLERBONUS",
                          "Quality Drilling Incentive (REV07) -- genuinely separate, paid in "
                          "addition to STM Bonus.", driller_sql, _run(cursor, driller_sql)))
        cols.append(_col(f"{gt}: Total Bonus", app_row.get("grand_stm", 0), "Derived", "n/a",
                          "= STM Bonus for this gang type.", is_formula=True))
    return cols


def _month_columns(cursor, period_from: int, period_to: int, section: str) -> list[dict]:
    periods = sorted((p for p in q.get_available_periods() if period_from <= p["value"] <= period_to),
                      key=lambda p: p["value"])
    sf = "" if section == "ALL" else f" AND SECTION = '{section}'"
    cols = []
    for p in periods:
        pv, label = p["value"], p["label"]
        sqm_sql = f"SELECT ROUND(SUM(SQM),2) AS value FROM #sqm WHERE PERIOD = '{pv}'{sf};"
        stm_sql = f"SELECT ROUND(SUM(STM),2) AS value FROM #participants WHERE PERIOD = '{pv}'{sf};"
        safety_sql = f"SELECT ROUND(SUM(SAFETY),2) AS value FROM #participants WHERE PERIOD = '{pv}'{sf};"
        driller_sql = f"SELECT ROUND(SUM(DRILLER),2) AS value FROM #participants WHERE PERIOD = '{pv}'{sf};"
        sqm_val = _run(cursor, sqm_sql) or 0.0
        stm_val = _run(cursor, stm_sql) or 0.0
        safety_val = _run(cursor, safety_sql) or 0.0
        driller_val = _run(cursor, driller_sql) or 0.0

        cols.append(_col(f"{label}: m\xb2", sqm_val, "GANGPRODUCTIONDETAIL",
                          "PERIOD, GANGTOTALSQMADJUSTED",
                          "STOPE BREAKING m\xb2 for this period only.", sqm_sql, sqm_val))
        cols.append(_col(f"{label}: STM Bonus", stm_val, "PARTICIPANTSDETAIL (deduplicated)",
                          "PERIOD, EMPLOYEESTOPETEAMBONUS",
                          "SUM(EMPLOYEESTOPETEAMBONUS) for this period only.", stm_sql, stm_val))
        cols.append(_col(f"{label}: Safety Bonus", safety_val, "PARTICIPANTSDETAIL (deduplicated)",
                          "PERIOD, EMPLOYEESAFETYBONUS",
                          "Informational only -- already included within STM Bonus.",
                          safety_sql, safety_val))
        cols.append(_col(f"{label}: Driller Bonus", driller_val, "PARTICIPANTSDETAIL (deduplicated)",
                          "PERIOD, EMPLOYEEDRILLERBONUS",
                          "Quality Drilling Incentive (REV07) for this period only.",
                          driller_sql, driller_val))
    return cols


# ── Workbook assembly ─────────────────────────────────────────────────────────

ROW_HEIGHTS = {1: 32, 2: 20, 3: 40, 4: 55, 5: 150, 6: 110, 7: 20, 8: 18}


def _write_value_sheet(wb: Workbook, title: str, columns: list[dict]) -> None:
    ws = wb.create_sheet(title)
    ws.freeze_panes = "A2"
    for r, height in ROW_HEIGHTS.items():
        ws.row_dimensions[r].height = height

    for ci, col in enumerate(columns, start=1):
        letter = get_column_letter(ci)
        ws.column_dimensions[letter].width = 42

        c1 = ws.cell(row=1, column=ci, value=col["label"])
        c1.font = HEADER_FONT
        c1.fill = HEADER_FILL
        c1.alignment = WRAP_TOP

        c2 = ws.cell(row=2, column=ci, value=col["value"])
        c2.font = VALUE_FONT
        c2.fill = VALUE_FILL
        c2.number_format = "#,##0.00"

        for r, key in ((3, "source"), (4, "columns"), (5, "explanation")):
            cell = ws.cell(row=r, column=ci, value=col[key])
            cell.font = BODY_FONT
            cell.alignment = WRAP_TOP

        c6 = ws.cell(row=6, column=ci, value=col["sql"] or "n/a -- see Explanation (derived value)")
        c6.font = MONO_FONT
        c6.alignment = WRAP_TOP

        sql_result = col["sql_result"]
        c7 = ws.cell(row=7, column=ci, value=sql_result if sql_result is not None else "n/a")
        c7.font = VALUE_FONT
        if sql_result is not None:
            c7.number_format = "#,##0.00"

        c8 = ws.cell(row=8, column=ci)
        if col["is_formula"] or sql_result is None:
            c8.value = "n/a"
        else:
            c8.value = f'=IF(ISNUMBER({letter}7),IF(ABS({letter}2-{letter}7)<=0.01,"YES","NO - check"),"n/a")'
        c8.font = MATCH_FONT


def _write_sql_setup_sheet(wb: Workbook, script: str) -> None:
    ws = wb.create_sheet("SQL Setup")
    ws.column_dimensions["A"].width = 120
    ws.cell(row=1, column=1,
            value="Copy all of column A (A3 down) into a new SSMS query window on the bonus "
                  "database and run it once. Then run any value's 'SQL to check it yourself' "
                  "(row 6 of the other sheets) in the same query window.").font = BODY_FONT
    ws.cell(row=1, column=1).alignment = WRAP_TOP
    for i, line in enumerate(script.splitlines()):
        ws.cell(row=3 + i, column=1, value=line).font = MONO_FONT


def _write_readme_sheet(wb: Workbook, period_from: int, period_to: int,
                         section: str, gangtype: str, generated_at: str) -> None:
    ws = wb.create_sheet("Read Me", 0)
    ws.column_dimensions["A"].width = 110
    lines = [
        ("Tshepong Stoping Analysis — Audit Pack", True),
        (f"Generated {generated_at} from database {DB_NAME} on {DB_SERVER}.", False),
        (f"Filters: periods {period_from}–{period_to}; section: {section}; "
         f"gang type: {gangtype}.", False),
        (f"Policy: {POLICY_REF}.", False),
        ("", False),
        ("What this is", True),
        ("Every value the dashboard shows for these filters, with where it comes from and how "
         "it was worked out, so it can be tested independently. Each value is one column. The "
         "rows are:", False),
        ("   Row 1: What the value is", False),
        ("   Row 2: Value — the number the dashboard shows (yellow). Some are Excel formulas "
         "built from other columns on the same sheet.", False),
        ("   Row 3: Source table(s)", False),
        ("   Row 4: Columns used", False),
        ("   Row 5: Explanation (how the value was worked out)", False),
        ("   Row 6: SQL to check it yourself — a query you can run yourself in SQL Server "
         "Management Studio.", False),
        ("   Row 7: SQL result at export — the result of that query when this pack was made, "
         "computed independently of the dashboard's own code.", False),
        ("   Row 8: Matches? — YES when row 2 and row 7 agree to within R0.01 (or 0.01 m\xb2).", False),
        ("", False),
        ("How to test", True),
        ("1. Open SQL Server Management Studio, connect to the bonus database and open a new "
         "query window.", False),
        ("2. Copy column A of the 'SQL Setup' sheet (from A3 down), paste it and run it. It "
         "builds two temporary tables (#participants, #sqm) that only exist in that window.", False),
        ("3. Copy any value's row-6 query, run it in the same window and compare with row 2.", False),
        ("4. To check the dedup/filter rules themselves, read the comments in the SQL Setup "
         "script — each rule (the GANGLINKEARN join fan-out, the GANG='xxx'/CREWNO='-' "
         "exclusions, the STOPE BREAKING scope) is written out there.", False),
        ("", False),
        ("Sheets", True),
        ("Headline Values — the KPI cards. By Section — m\xb2 and bonus per section. "
         "By Gang Type — bonus per gang type. By Month — m\xb2 and bonus per period. "
         "SQL Setup — the script to run first.", False),
        ("", False),
        ("Notes", True),
        ("• STM Bonus is what payroll actually paid (EMPLOYEESTOPETEAMBONUS in "
         "PARTICIPANTSDETAIL), not a production-formula estimate.", False),
        ("• Safety Bonus is shown for visibility but is already included inside STM Bonus "
         "— do not add it on top.", False),
        ("• Driller Bonus (Quality Drilling, REV07) is genuinely separate and is NOT "
         "included in Total Bonus/STM Bonus anywhere in this pack — shown as its own figure "
         "throughout.", False),
        ("• m\xb2 is measured per STOPE BREAKING gang and is not meaningful split by gang "
         "type, so the By Gang Type sheet has no m\xb2 column.", False),
        ("• By Section and By Gang Type's row 2 and row 7 both come from this same SQL "
         "query — they are expected to match exactly because that SQL IS the dashboard's "
         "source of truth for those breakdowns; Headline Values' row 2 comes from a separate "
         "Python aggregation, so its match is a genuine independent cross-check.", False),
    ]
    for r, (text, bold) in enumerate(lines, start=1):
        c = ws.cell(row=r, column=1, value=text)
        c.font = Font(name="Arial", bold=bold, size=11 if bold and r == 1 else 10)
        c.alignment = WRAP_TOP
        ws.row_dimensions[r].height = 30 if len(text) > 90 else 16


def build_audit_pack(period_from: int, period_to: int,
                      section: str = "ALL", gangtype: str = "ALL") -> BytesIO:
    """Returns an in-memory .xlsx workbook. Does not touch disk.

    Opens ONE pyodbc connection for the whole build (via get_connection()) so the
    #participants/#sqm temp tables created by the setup script stay visible to
    every check query that follows -- see module docstring.
    """
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(_sql_setup_script(period_from, period_to))

        headline = _headline_columns(cursor, period_from, period_to, section, gangtype)
        by_section = _section_columns(cursor, period_from, period_to)
        by_gangtype = _gangtype_columns(cursor, period_from, period_to, section)
        by_month = _month_columns(cursor, period_from, period_to, section)

    wb = Workbook()
    wb.remove(wb.active)  # drop the default blank sheet

    _write_readme_sheet(wb, period_from, period_to, section, gangtype, generated_at)
    _write_value_sheet(wb, "Headline Values", headline)
    _write_value_sheet(wb, "By Section", by_section)
    _write_value_sheet(wb, "By Gang Type", by_gangtype)
    _write_value_sheet(wb, "By Month", by_month)
    _write_sql_setup_sheet(wb, _sql_setup_script(period_from, period_to))

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
