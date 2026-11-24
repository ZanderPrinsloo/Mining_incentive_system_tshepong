"""Reads the monthly BASE tables instead of the database views.

The server database has no views (GANGPRODUCTIONDETAIL, PARTICIPANTSDETAIL, PRODUCTIONWPDETAIL do
not exist there), only the monthly tables the views were built from:

  GANGPRODUCTIONDETAIL = GANGLINKEARN<p> LEFT JOIN PRODUCTIONEARN<p>   (WHERE GANG != 'XXX')
  PARTICIPANTSDETAIL   = PARTICIPANTSEARN<p> LEFT JOIN GANGLINKEARN<p> (CREWNO, ancillary filters)
  PRODUCTIONWPDETAIL   = PRODUCTIONEARN<p>

This module rebuilds exactly those three row sets from the tables, so every query in the app can keep
using the three names. ``expand_views(sql)`` puts them in front of a statement as common table
expressions (``WITH GANGPRODUCTIONDETAIL AS (...)``); ``view_subquery(name, ...)`` gives the same
thing as a derived table for the SQL scripts that are shown to people.

Which months exist is read from the database (tables named <BASE>YYYYMM), so a new month appears by
itself. The views started in July 2025, and so does this. The RGM rate, B-Reef stoping-width factor and
dip factor come from the tables for the months that have those columns (Jan-26 and Apr-26 on); for the
other months the views used a constant '1', and so does this.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

FIRST_PERIOD = 202507            # the views start here
VIEW_NAMES = ("GANGPRODUCTIONDETAIL", "PARTICIPANTSDETAIL", "PRODUCTIONWPDETAIL")

_CACHE_SECONDS = 300
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "months": [], "cols": set()}


def _load(run) -> None:
    """Fill the month / column cache. ``run(sql)`` executes a SELECT and returns a DataFrame."""
    tables = run("""SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'
                    AND (TABLE_NAME LIKE 'GANGLINKEARN2%' OR TABLE_NAME LIKE 'PRODUCTIONEARN2%'
                         OR TABLE_NAME LIKE 'PARTICIPANTSEARN2%')""")
    have = set(tables["TABLE_NAME"].astype(str).str.strip().str.upper()) if not tables.empty else set()
    months = sorted(
        int(m) for m in {t[-6:] for t in have if t[-6:].isdigit()}
        if all(f"{b}{m}" in have for b in ("GANGLINKEARN", "PRODUCTIONEARN", "PARTICIPANTSEARN"))
        and int(m) >= FIRST_PERIOD)
    cols = run("""SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                  WHERE COLUMN_NAME IN ('WORKPLACERGMRATE', 'B_REEF_SW_FACTOR', 'DIP_FACTOR')
                    AND (TABLE_NAME LIKE 'GANGLINKEARN2%' OR TABLE_NAME LIKE 'PRODUCTIONEARN2%')""")
    colset = ({(str(r.TABLE_NAME).strip().upper(), str(r.COLUMN_NAME).strip().upper()) for r in cols.itertuples()}
              if not cols.empty else set())
    _cache.update(at=time.time(), months=months, cols=colset)


def _ensure(run=None) -> None:
    with _lock:
        if _cache["months"] and time.time() - _cache["at"] < _CACHE_SECONDS:
            return
        if run is None:
            from src.database.connection import read_sql_plain as run   # no view expansion for these lookups
        _load(run)


def existing_months(period_from: Optional[int] = None, period_to: Optional[int] = None) -> list[int]:
    """Months that have all three monthly tables, oldest first."""
    _ensure()
    return [m for m in _cache["months"]
            if (period_from is None or m >= period_from) and (period_to is None or m <= period_to)]


def _has(table: str, col: str) -> bool:
    return (table.upper(), col.upper()) in _cache["cols"]


def _month_select(name: str, p: int) -> str:
    if name == "GANGPRODUCTIONDETAIL":
        real = _has(f"GANGLINKEARN{p}", "WORKPLACERGMRATE") and _has(f"GANGLINKEARN{p}", "B_REEF_SW_FACTOR")
        rgm = "t1.WORKPLACERGMRATE" if real else "'1'"
        sw = "t1.B_REEF_SW_FACTOR" if real else "'1'"
        return (f"SELECT t1.BUSSUNIT, t1.SECTION, t1.PERIOD, t1.WORKPLACE, t1.WORKPLACE_NAME, "
                f"t2.STOPESQM AS WPSTOPESQM, t2.LEDGESQM AS WPLEDGESQM, t1.WORKPLACETOTALSQM, t1.GANG, t1.GANGTYPE, t1.CREWNO, "
                f"t1.CREWSHIFTS, t1.GANGTOTALSQMADJUSTED, t1.GANGLABOUR, t1.GANGEFFICIENCY, "
                f"t1.GANGFULLSTOPINGBONUS AS GANGEFFICIENCYBONUS, t1.WORKPLACENETTINGRATE, {rgm} AS WORKPLACERGMRATE, "
                f"{sw} AS B_REEF_SW_FACTOR, t1.GANGWPSTOPINGBONUS, t1.GANGFINALEFFICIENCYBONUS AS GANGFINALBREAKBONUS, "
                f"t1.GANGDRILLERBONUS, t1.GANGPRODUCTIONBONUS AS GANGPRODUCTIONBONUS, t1.GANGFINALSWEEPINGSBONUS, "
                f"t1.GANGFINALSAFETYBONUS, t1.GANGLTIIND, t1.GANGDRESSINGIND, t1.GANGFATALIND "
                f"FROM dbo.[GANGLINKEARN{p}] t1 LEFT JOIN dbo.[PRODUCTIONEARN{p}] t2 "
                f"ON t1.WORKPLACE = t2.WORKPLACE AND t1.SECTION = t2.SECTION AND t1.PERIOD = t2.PERIOD "
                f"WHERE t1.GANG != 'XXX'")
    if name == "PARTICIPANTSDETAIL":
        return (f"SELECT t1.BUSSUNIT, t1.SECTION, t1.PERIOD, t1.EMPLOYEE_NO, ISNULL(t2.CREWNO, 'ANCILLARY') AS CREWNO, "
                f"t1.GANG, t1.GANGTYPE, t1.WAGECODE, t1.WAGE_DESCRIPTION, t1.EMPLOYEESUPERVISIONRATE, "
                f"t1.EMPLOYEESTOPETEAMBONUS, t1.EMPLOYEESAFETYBONUS, t1.EMPLOYEEDRILLERBONUS, t1.EMPLOYEEAWOPPENALTY "
                f"FROM dbo.[PARTICIPANTSEARN{p}] t1 LEFT JOIN dbo.[GANGLINKEARN{p}] t2 "
                f"ON t1.SECTION = t2.SECTION AND t1.PERIOD = t2.PERIOD AND t1.GANG = t2.GANG "
                f"WHERE SUBSTRING(t1.WAGECODE, 1, 3) != '246' AND SUBSTRING(t1.WAGECODE, 1, 1) != '3' AND t1.EMPLOYEE_NO != '0'")
    if name == "PRODUCTIONWPDETAIL":
        dip = "ISNULL(DIP_FACTOR, '1')" if _has(f"PRODUCTIONEARN{p}", "DIP_FACTOR") else "'1'"
        return (f"SELECT 'JB' AS BUSSUNIT, PERIOD, SECTION, WORKPLACE, LEDGESQM AS WPMAXLEDGESQM, STOPESQM AS WPMAXSTOPESQM, "
                f"WORKPLACERATETYPE, WORKPLACESWEEPINGSDISTANCE, WORKPLACETOTALFA, {dip} AS DIP_FACTOR, "
                f"WORKPLACETOTALSQM AS WPPRETOTALM2, WORKPLACETOTALSQMADJUSTED AS WPTOTALM2 FROM dbo.[PRODUCTIONEARN{p}]")
    raise KeyError(name)


def view_body(name: str, period_from: Optional[int] = None, period_to: Optional[int] = None) -> str:
    """The SELECT ... UNION ALL ... that equals the view, over the monthly tables in the range."""
    months = existing_months(period_from, period_to)
    if not months:
        raise RuntimeError(f"No monthly tables found for {name} (GANGLINKEARN/PRODUCTIONEARN/PARTICIPANTSEARN<YYYYMM>).")
    return "\nUNION ALL\n".join(_month_select(name, p) for p in months)


def view_subquery(name: str, period_from: Optional[int] = None, period_to: Optional[int] = None) -> str:
    """The view as a derived table, aliased with its own name: ``FROM (...) PARTICIPANTSDETAIL``."""
    return f"(\n{view_body(name, period_from, period_to)}\n) {name}"


_NAME_RE = {n: re.compile(rf"(?<![\w.#@])\[?{n}\]?(?!\w)", re.I) for n in VIEW_NAMES}


def expand_views(sql: str) -> str:
    """Put the views a statement uses in front of it as CTEs built from the base tables."""
    used = [n for n in VIEW_NAMES if _NAME_RE[n].search(sql)]
    if not used:
        return sql
    defs = ",\n".join(f"{n} AS (\n{view_body(n)}\n)" for n in used)
    body = sql.lstrip()
    if re.match(r"WITH\b", body, re.I):                 # the statement has CTEs of its own: add ours first
        return f"WITH {defs},\n" + body[4:].lstrip()
    return f"WITH {defs}\n" + body
