# Self-Healing Network

Flask dashboard with failure detection, alternative-path recovery, monitoring and
an inline-SVG network topology.

## Databases

Three databases, never mixed up. The path comes from one place, `db_config.py`,
read from the `SHN_DATABASE_PATH` environment variable.

| File | Used by | Notes |
| --- | --- | --- |
| `network.db` | nobody, by default | Your existing data. Nothing defaults to it. |
| `network_dev.db` | `python app.py` | Development. Created and auto-initialised on first run. |
| `%TEMP%\shn-tests-*\network.db` | `python run_tests.py` | One throwaway database per test run, deleted afterwards. |

`SHN_DATABASE_PATH` is **required**. If it is missing or unusable the app stops
with an explanation instead of quietly opening `network.db`.

## Running the development server

```powershell
cd C:\SelfHealingNetwork
python app.py
```

`app.py` configures `network_dev.db` for you, creates any missing tables, and
prints the resolved path:

```
Database in use: C:\SelfHealingNetwork\network_dev.db
```

Then open <http://127.0.0.1:5000>. Monitoring starts automatically and writes to
`network_dev.db`, so experimenting with Simulate Failure / Recover Network cannot
change `network.db`.

To choose a different file:

```powershell
$env:SHN_DATABASE_PATH = "C:\SelfHealingNetwork\network_dev.db"
python app.py
```

### Working on `network.db` deliberately

Only if you really want the app to write to your existing data:

```powershell
$env:SHN_DATABASE_PATH = "C:\SelfHealingNetwork\network.db"
python app.py
```

This logs a warning naming `network.db` as the production database on every
start, so the choice is never silent.

## Running the tests

```powershell
cd C:\Users\acer\AppData\Local\Temp\kilo\jstest
python run_tests.py            # every suite
python run_tests.py failed_hosts   # one suite
```

The runner creates a fresh temporary database, starts its own server on port
5099 with no debug reloader, and deletes the database afterwards. It reads
`network.db` read-only to prove it is unchanged, and **fails the run if any table
moved**, naming the suite responsible. Never run a suite directly: each one
refuses to start unless `SHN_TEST_BASE` and `SHN_DATABASE_PATH` point at the
isolated environment.

## Layout

| File | Role |
| --- | --- |
| `db_config.py` | Resolves and validates the database path. |
| `database.py` | The only place a SQLite connection is opened. |
| `network_topology.py` | Graph model, node coordinates, health calculation. |
| `failure_detector.py` | Monitoring loop and failure events. |
| `recovery_manager.py` | Alternative-path recovery. |
| `app.py` | Flask routes and entry point. |