# Deploying the Tshepong Dashboard to a company server

The app is a Flask dashboard (Waitress WSGI server) that reads from the SQL
Server reporting database. This guide deploys it as a **Windows service** that
starts automatically on boot and is reachable on the intranet at
`http://<server>:5001`.

---

## Before you start

You need on the server:

1. **Python 3.12+** installed for your user (Python 3.14.6 is known to work).
2. **ODBC Driver 17 for SQL Server** — the app talks to SQL Server via ODBC.
   Check with:
   ```bat
   reg query "HKLM\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server"
   ```
   If missing, install it from
   https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
   (ask IT if you don't have rights).
3. **Network access** from the server to the SQL Server host (by default
   `HGSQLHRT001`, see below).
4. The **`.env` file** with the database credentials. It is NOT in git
   (secrets) — copy it from your dev machine to the project folder on the
   server. Content:
   ```
   DB_SERVER=HGSQLHRT001
   DB_USERNAME=icalc_test_user
   DB_PASSWORD=password08
   ```

---

## Deployment steps

Copy the whole project folder to the server (e.g. `C:\inetpub\tshepong` or
any folder you can write to). Then on the server:

### 1. One-time setup (elevated Command Prompt)

```bat
cd C:\<path-to-project>
deploy\setup.bat
```

This creates the virtual environment, installs dependencies (`requirements`
+ `pywin32`), and opens firewall port 5001. Needs internet for pip.

### 2. Install and start the service

```bat
deploy\install_service.bat
```

By default the service runs as **LocalSystem**. The app authenticates to SQL
Server with the login in `.env`, so LocalSystem is fine. If you must run it
as your own account instead:

```bat
deploy\install_service.bat DOMAIN\youruser yourpassword
```

### 3. Verify

On the server: open `http://localhost:5001`.
From another PC: open `http://<server-name>:5001`.

To uninstall: `deploy\uninstall_service.bat`.

---

## Day-to-day operations

| Task                     | Command                                                      |
| ------------------------ | ------------------------------------------------------------ |
| Start service            | `.venv\Scripts\python.exe run_service.py start`              |
| Stop service             | `.venv\Scripts\python.exe run_service.py stop`               |
| Restart service          | `.venv\Scripts\python.exe run_service.py restart`            |
| Run in foreground (test) | `deploy\start.bat`  (stop with `deploy\stop.bat`)            |
| Check logs               | `logs\` folder in the project root                           |

The service appears as **"Tshepong Stoping Analysis Dashboard"** in
services.msc.

---

## Updating to a new version

```bat
cd C:\<path-to-project>
git pull
deploy\setup.bat                      :: re-installs any new dependencies
.venv\Scripts\python.exe run_service.py restart
```

---

## Configuration

`config\config.yaml` and the `.env` file control behaviour:

- `DB_SERVER` / `DB_DATABASE` — SQL Server instance and database.
- `DB_USERNAME` / `DB_PASSWORD` — SQL login (leave unset for Windows auth).
- `HOST` / `PORT` — defaults `0.0.0.0:5001`; override via environment
  variables if 5001 conflicts.
- Dashboard targets and saved scenarios are stored locally in
  `data\dashboard_state.db` (SQLite) — back this file up if you care about
  them.

## Troubleshooting

- **`http://<server>:5001` works on the server but not from other PCs** —
  firewall rule missing or blocked by corporate policy. Re-run `deploy\setup.bat`
  elevated, or have IT open TCP 5001.
- **Pages load but show errors in the charts** — check the SQL Server is
  reachable from the server: `ping HGSQLHRT001` and verify the ODBC driver
  version matches `config\config.yaml` (`driver:`).
- **Service won't start** — check Windows Event Viewer (Application log,
  source `TshepongDashboard`); recent errors are also in `logs\`.
