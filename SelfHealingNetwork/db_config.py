"""Single source of truth for which SQLite file this process talks to.

The project has three databases and they must never be confused:

    network.db       the original database. Treated as production: read-only as
                     far as the app is concerned, and never a default.
    network_dev.db   what `python app.py` uses, so running the development
                     server cannot touch the production data.
    <temp dir>\\...\\network.db   what run_tests.py uses, one per test run.

Every module reads its path from here, so app.py, database.py, failure_detector.py
and recovery_manager.py can never disagree about which file they are on.

Configuration is via the SHN_DATABASE_PATH environment variable. It is
deliberately required: when it is missing or unusable this module raises a clear
error rather than quietly falling back to network.db.
"""
import logging
import os

logger = logging.getLogger(__name__)

ENV_VAR = "SHN_DATABASE_PATH"
PRODUCTION_DB_NAME = "network.db"
DEVELOPMENT_DB_NAME = "network_dev.db"

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Shown when SHN_DATABASE_PATH is missing, so the fix is obvious.
_MISSING = f"""\
{ENV_VAR} is not set, so there is no database to use.

This project never falls back to {PRODUCTION_DB_NAME!r} on purpose: that file holds
your existing data. Pick a database explicitly:

  development server (uses {DEVELOPMENT_DB_NAME}, separate from {PRODUCTION_DB_NAME}):
      set {ENV_VAR}=<project dir>\\{DEVELOPMENT_DB_NAME}
    or just run `python app.py`, which configures this for you.

  tests (creates and deletes a throwaway database per run):
      set {ENV_VAR}=<some temp dir>\\network.db
    or just run `python run_tests.py` in the test folder.

To work on {PRODUCTION_DB_NAME} itself, set {ENV_VAR} to it explicitly and accept
that the app will write to it.
"""


class DatabaseConfigurationError(RuntimeError):
    """Raised when the database path is missing, unusable or not writable."""


def resolve_database_path(env=None):
    """The absolute database path for this process.

    Raises DatabaseConfigurationError when SHN_DATABASE_PATH is absent, blank,
    points at a directory, or sits in a folder that does not exist. It never
    returns a default.
    """
    environ = os.environ if env is None else env
    raw = environ.get(ENV_VAR)

    if raw is None or not str(raw).strip():
        raise DatabaseConfigurationError(_MISSING)

    path = os.path.abspath(os.path.expanduser(str(raw).strip()))

    if os.path.isdir(path):
        raise DatabaseConfigurationError(
            f"{ENV_VAR}={path!r} points at a directory, not a SQLite file.")

    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise DatabaseConfigurationError(
            f"{ENV_VAR}={path!r} cannot be used: the folder {parent!r} does not "
            "exist. Create it first, or point the variable somewhere writable.")

    if not os.access(parent, os.W_OK):
        raise DatabaseConfigurationError(
            f"{ENV_VAR}={path!r} cannot be used: the folder {parent!r} is not "
            "writable, so the app could not record failures or recoveries.")

    # An explicit choice is allowed, but it must never be a silent one. Only the
    # project's own network.db counts: the test harness deliberately creates a
    # throwaway file that is also called network.db, and warning about that on
    # every run would be noise.
    if is_production_database(path):
        logger.warning(
            "%s points at the production database %s. Everything this process "
            "writes (failures, recoveries, monitoring logs) will go there.",
            ENV_VAR, path)

    return path


def use_development_database(base_dir=None, override=False):
    """Point this process at the development database.

    Sets SHN_DATABASE_PATH to <base_dir>/network_dev.db. An existing value is
    kept unless override is True, so an explicit setting (the test harness, or a
    deliberate production run) always wins over the development default.

    Returns the resolved path.
    """
    existing = os.environ.get(ENV_VAR)
    if existing and not override:
        return resolve_database_path()

    target_dir = os.path.abspath(base_dir) if base_dir else PROJECT_DIR
    os.makedirs(target_dir, exist_ok=True)
    os.environ[ENV_VAR] = os.path.join(target_dir, DEVELOPMENT_DB_NAME)
    return resolve_database_path()


def is_production_database(path):
    """True when `path` is the project's original network.db."""
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(
        os.path.join(PROJECT_DIR, PRODUCTION_DB_NAME))


def describe():
    """Human-readable summary for startup logging."""
    path = resolve_database_path()
    kind = "PRODUCTION" if is_production_database(path) else os.path.basename(path)
    return f"{path}  [{kind}]"