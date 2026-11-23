# Deploying the Tshepong Dashboard to a company server

The app is a Flask dashboard served by **Waitress** that reads the bonus database **STPTM4000** on SQL Server.
This guide installs it as a **Windows service** that starts automatically on boot and is reachable on the intranet
at `http://<server>:5001` (Doornkop 5000, Tshepong 5001, Pakisa 5002, Mponeng 5003).

---

## Before you start

You need on the server:

1. **Python 3.12+, installed "for all users"** (e.g. `C:\Program Files\Python312`). The service runs the real
   `python.exe` behind the `.venv`, so the service account must be able to read that Python folder — a per-user install
   under someone's `AppData` usually fails for a service account.
2. **ODBC Driver 17 for SQL Server** — the app talks to SQL Server via ODBC. Check with:
   ```bat
   reg query "HKLM\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server"
   ```
   If missing, install it from
   https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
   (ask IT if you don't have rights).
3. **Network access** from the server to the SQL Server host.
4. The **`.env` file** with the database login. It is NOT in git (secrets) — copy it from your dev machine to the
   project folder on the server. Content:
   ```
   DB_SERVER=<sql-server>\<instance>
   DB_USERNAME=<sql login>
   DB_PASSWORD=<password>
   ```
   The database name (`STPTM4000`) is set by the app itself. The dashboard only reads — no stored procedures or
   extra database objects are needed.

---

## Deployment steps

Copy the whole project folder to the server (e.g. `C:\inetpub\tshepong`). Then, on the server:

### 1. One-time setup (elevated Command Prompt)

```bat
cd C:\<path-to-project>
deploy\setup.bat
```

Creates the virtual environment, installs `requirements.txt` (incl. Waitress and openpyxl) + `pywin32`, and opens
firewall port 5001. Needs internet for pip.

### 2. Install and start the service

```bat
deploy\install_service.bat
```

By default the service runs as **LocalSystem**. The app authenticates to SQL Server with the login in `.env`, so
LocalSystem is fine. To run it as a specific account instead:

```bat
deploy\install_service.bat DOMAIN\youruser yourpassword
```

### 3. Verify

On the server: `http://localhost:5001`. From another PC: `http://<server-name>:5001`.

To uninstall: `deploy\uninstall_service.bat`.

---

## Day-to-day operations

| Task | Command |
|---|---|
| Start service | `deploy\start_service.bat` (or `.venv\Scripts\python.exe run_service.py start`) |
| Stop service | `deploy\stop_service.bat` (or `.venv\Scripts\python.exe run_service.py stop`) |
| Restart service | `.venv\Scripts\python.exe run_service.py restart` |
| Run in the foreground (test) | `.venv\Scripts\python.exe run_server.py` (Ctrl+C to stop) |
| On your own PC | `python run_web.py` (Flask debug server, auto-reload) or press **F5** in VS Code |

The service is **`iCalcTshepongDashboard`** — shown as **"iCalc Tshepong Stoping Analysis Dashboard"** in
services.msc (search for "iCalc"). Every .bat file and every `run_service.py` command checks for administrator
rights first.

---

## Updating to a new version

```bat
cd C:\<path-to-project>
git pull
deploy\setup.bat
.venv\Scripts\python.exe run_service.py restart
```

---

## Configuration

`config\config.yaml` and the `.env` file control behaviour:

- `DB_SERVER` / `DB_DATABASE` — SQL Server instance and database.
- `DB_USERNAME` / `DB_PASSWORD` — SQL login (leave unset for Windows auth).
- `HOST` / `PORT` — defaults `0.0.0.0:5001`; override via environment variables if 5001 conflicts.
- Dashboard targets and saved scenarios are stored locally in `data\dashboard_state.db` (SQLite) — back this file up
  if you care about them.

---

## Troubleshooting

- **Works on the server but not from other PCs** — firewall: re-run `deploy\setup.bat` elevated, or ask IT to
  open TCP 5001.
- **Pages load but show errors in the charts** — check the SQL Server is reachable from the server and that the ODBC
  driver matches `config\config.yaml` (`driver:`).
- **Service won't start / error 1053** — read `logs\service.log` first, then Event Viewer → Application log (source
  `iCalcTshepongDashboard`). The service reports "running" immediately and loads the dashboard afterwards, so 1053
  points to Python itself not starting: check the Python install is "for all users" and that `deploy\setup.bat` ran
  (it installs pywin32). Reinstall after moving the folder: `uninstall_service.bat` then `install_service.bat`.
- **Replacing the old service** — if the previous `TshepongDashboard` service is installed, remove it first:
  `sc delete TshepongDashboard`.
- **Port already used** — set `PORT=<other>` as a system environment variable for the service (and open that port).
