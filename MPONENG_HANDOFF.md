# Mponeng Mining Bonus Incentive Dashboard — New Project Handoff

Paste this whole document as your first message in a new Claude Code session, in a new project folder, along with Mponeng's own bonus policy document(s) attached. It gives Claude the architecture, design, and working method from the Tshepong system — but none of Tshepong's policy numbers, formulas, or database names, since Mponeng's policy and database are different and must be read fresh from what you attach.

---

## 1. What you're building

A Flask-based mining bonus incentive dashboard for **Mponeng mine**, matching the design, structure, and rigor of an existing sibling system built for Tshepong (and Pakisa, which shares Tshepong's policy). Mponeng has its **own bonus policy** — do not assume any formula, percentage, threshold, or column name from Tshepong carries over. Everything policy-specific must come from the Mponeng policy document(s) I attach and from Mponeng's own database, verified empirically, not assumed.

## 2. Tech stack and project structure (replicate this)

- **Flask** app (`web/app.py`, `run_web.py`), server-rendered single-page dashboard (`web/templates/index.html`) with vanilla JS (no frontend framework) and Chart.js/DataTables for visuals and tables.
- **SQL Server** backend via SQLAlchemy + pyodbc. All query logic lives in `web/queries.py` as plain functions returning dicts/lists, called from Flask routes in `app.py`, called from `fetch`-based JS in the template.
- **Config layering**, in this precedence: `config/config.yaml` (committed defaults, e.g. local Windows-authenticated dev) → `.env` (gitignored, local machine overrides) loaded via `python-dotenv` in `config/settings.py`. This lets one person's local dev machine and a shared/remote deployment both work from the same codebase without code changes — only `.env` differs per machine.
- **Git**: initialize a repo, connect to a GitHub remote, commit only non-secret files (`.env` stays gitignored; commit a `.env.example` only if asked).

## 3. Dashboard design (replicate this UX)

A single page with a top filter bar (period range, section, gang type) driving every section below it:

1. **KPI cards**: Total m², Total Bonus, Gang Count, Avg m²/gang, R/m² — each with a period-over-period delta badge.
2. **Production trend charts**: adjusted vs pre-adjustment m² over time, by section.
3. **Bonus Analysis**: bonus broken down by gang type and period, with whatever real sub-components the Mponeng data actually supports (don't assume STM/Safety/Driller — find out what Mponeng's payroll table actually separates out).
4. **Gang/Workplace Detail table**: DataTables-powered, sortable, filterable, workplace-level drill-down per gang, with outlier detection (flag rows whose bonus/m² is far from the period average).
5. **Section Performance Ranking**: sections ranked by R/m².
6. **SQM range distribution**: gangs bucketed by m² achieved, with bonus breakdown per bucket.
7. **Forecast**: simple linear regression + seasonal index projection on historical bonus/production.
8. **Bonus Policy Simulator**: the most complex piece — lets the user move policy levers (whatever Mponeng's policy actually has — entry thresholds, rate uplifts, safety ladders, caps, whatever REV document says) and see a "Current vs Proposed" comparison, annualized impact, and a section breakdown table. Build this **last**, after the KPI/Bonus Analysis numbers are independently verified correct, and anchor every "Current" figure in it to real payroll, never to a production-formula estimate alone.

## 4. Critical working method — apply this to Mponeng's own data, from scratch

These are hard-won lessons from building the Tshepong system. They are **methodology**, not facts about Mponeng — re-verify every one of them against Mponeng's actual database before trusting anything derived from it.

1. **Find Mponeng's canonical real-payroll table/view first**, separate from any production-formula estimate table. Treat the estimate table (whatever computes m² × rate × gang headcount) as an *estimate only* — it will diverge from real payroll, sometimes by double digits of percent. Anchor every dollar figure shown to the user to real payroll, not the estimate, unless there's no real-payroll source at all.

2. **Never trust a view's row count at face value.** If a payroll view is built as a UNION ALL across per-period tables joined to a link/gang table, check for join fan-out: compare `COUNT(*)` against `COUNT(DISTINCT <natural key>)` for at least one real period before writing any aggregation query against it. If they differ, find the real natural key by testing candidates against a hand-computed "true" count — don't guess a dedup key and move on.

3. **Never assume a "total" column equals the sum of columns that look like its components.** If the source system has its own pre-computed total column (even if the app's view doesn't expose it — check the raw base tables), pull it and reconcile your constructed formula against it, per individual record, across several real periods, before using the constructed formula anywhere. In Tshepong's case this caught a real double-counting bug: a "safety bonus" column that looked additive was actually already included in another column.

4. **When building a policy simulator**, don't assume your reconstruction formula is correct just because it reproduces the real number exactly at the current policy's default settings — that can happen by pure algebraic construction (dividing out a constant then multiplying it back in cancels regardless of whether the constant is right). Prove correctness by checking the formula's *rate* against real data directly (e.g. does a specific named bonus component actually divide out to the percentage the policy document claims, across many real records — not just one).

5. **For simulator levers that represent a rate/percentage**, prefer computing the *point-change* the user dials in and applying it as an increment to each real entity's own real number, over reconstructing a "true underlying base" and re-deriving from scratch. The increment approach sidesteps needing to know the exact true formula and is far more robust — this is what Tshepong's Safety % lever ended up using after an earlier, more "principled"-looking approach turned out to be wrong.

6. **Keep simulator lever families independent.** A shortcut that passes real values straight through "when everything is at default" must be scoped per lever family (e.g. efficiency-affecting levers vs safety-affecting levers), not as one global on/off switch — otherwise touching an unrelated lever can leak unrelated reconstruction noise into totals it shouldn't touch.

7. **Triple-check everything against real data before presenting a number.** Write small standalone verification queries (via `sqlcmd` or similar) that are independent of the app's own code, and reconcile to the cent wherever possible, especially for anything described to the user as a "total." If the user asks you to double-check something, actually requery from scratch rather than re-deriving from a cached assumption.

## 5. What I'll provide in the new session

- Mponeng's bonus policy document(s) (REV number, effective date — treat it the same way REV04 was treated for Tshepong: the authoritative source for every threshold/percentage/formula).
- Mponeng's database connection details (server instance, database name — equivalent to `STPTM4000`/`localhost\MSSQLSERVER01` for Tshepong).
- Confirmation of which views/tables hold production data vs real payroll data once you've explored the schema.

## 6. What to do first in the new session

1. Set up the Flask skeleton, config layering, and git repo (mirroring section 2).
2. Explore Mponeng's database schema — list tables/views, identify the production-estimate table and the real-payroll table (or confirm if only one exists).
3. Read the attached Mponeng policy document(s) fully before writing any bonus formula.
4. Build KPI/Bonus Analysis first, verify every total per the methodology in section 4, *then* build the Simulator.
5. Match the Tshepong dashboard's visual layout and interaction patterns (section 3) once the data layer is verified — don't let UI work get ahead of verified numbers.

Do not ask me to confirm this plan — get started on steps 1-2 once you have my database details, and pause to ask only where Mponeng's policy document is genuinely ambiguous or where the schema doesn't have an obvious equivalent to something described above.
