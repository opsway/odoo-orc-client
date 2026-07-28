"""Kill a wedged Odoo load instead of letting it hang until the build times out.

On 2026-07-28 a gourmetfoods **production** build stopped dead in STEP 9 of the
module loader (``_register_hook`` on every model, ``odoo/modules/loading.py``)
and emitted nothing further until Odoo.sh rejected it at the ~2 hour build
limit. Two hours of silence, no traceback, no way in: a build in progress on
Odoo.sh offers neither a shell nor psql, so nothing can be inspected *while* it
hangs. The only channel out of a building container is its log stream, and the
only code that can write to that is code running inside the process.

Hence this watchdog. It runs from ``post_load`` — the earliest hook available,
before any ``_register_hook`` — and if module loading stops making progress it
dumps every thread's stack to stderr and then aborts the process, so the build
fails in minutes with evidence attached rather than in hours with none.

Three design constraints, each learned the hard way:

* **Never call ``Registry(db)``.** ``Registry.__new__`` acquires
  ``Registry._lock`` (``odoo/modules/registry.py:104``), which ``Registry.new()``
  holds for the entire load. A thread that does this parks until shutdown — that
  is exactly what strands ``orc_client_build_reporter``'s reporting thread and
  why it has never once reported from Odoo.sh. We read ``Registry.registries``
  directly instead: the registry is published there at ``registry.py:124``
  *before* ``load_modules()`` runs, and ``ready`` flips at ``:144``, so the pair
  is a reliable "still loading?" signal that takes no lock.
* **Never use ``registry.cursor()``.** Same lock. SQL goes through
  ``odoo.sql_db.db_connect`` (``sql_db.py:843``), which hands out a pooled
  psycopg2 connection and touches no registry lock.
* **Stall, not elapsed time.** Killing on wall-clock would murder legitimately
  slow builds (a big data migration on a large database). We watch
  ``pg_stat_activity`` instead: a backend that is ``active`` and not waiting on a
  Lock is *working*, and working resets the clock however long it takes. Only
  when nothing is working — every backend idle in a transaction, or blocked on a
  lock — does the stall counter advance. That distinguishes "slow" from "wedged".
* **SQL is not the only kind of progress.** A migration or hook doing heavy
  *Python* work between statements leaves its connection ``idle in transaction``
  with an unchanged snapshot — indistinguishable, to Postgres, from a wedge. So
  consumed CPU counts as progress too. Every hang this guards against burns no
  CPU: a lock wait, an untimed socket read and a ``sleep`` are all parked in the
  kernel. Real computation is not, so it keeps resetting the clock.

Silent by default: a healthy load finishes in seconds (gourmetfoods staging:
``Registry loaded in 12.665s``), so at a 5 minute dump threshold this never
speaks in normal operation.

Tunable through either an environment variable or an ``odoo.conf`` key
(env wins), which matters because Odoo.sh offers neither — on Odoo.sh the
constants below are what you get, and they are chosen to be safe unattended:

    ORC_LOAD_WATCHDOG_POLL_SECONDS   / orc_load_watchdog_poll_seconds
    ORC_LOAD_WATCHDOG_DUMP_SECONDS   / orc_load_watchdog_dump_seconds
    ORC_LOAD_WATCHDOG_ABORT_SECONDS  / orc_load_watchdog_abort_seconds  (0 disables the abort)
"""

import faulthandler
import logging
import os
import sys
import threading
import time

from odoo import sql_db
from odoo.modules.registry import Registry
from odoo.tools import config

_logger = logging.getLogger(__name__)

# Seconds between polls.
POLL_SECONDS = 30
# Stall duration that triggers a stack dump (and every multiple thereafter).
DUMP_SECONDS = 300
# Stall duration that aborts the process. 0 disables the abort, leaving dumps.
# Deliberately well under Odoo.sh's ~2h build limit and ~70x a healthy load.
ABORT_SECONDS = 900
# Consecutive polls with no registry load to watch before the watchdog gives up.
ABSENT_GIVE_UP_POLLS = 10
# CPU seconds the process must burn between two polls to count as "working"
# without any SQL activity. A busy interpreter spends most of the interval on
# CPU; a parked one spends effectively none, so anything above noise will do.
CPU_PROGRESS_SECONDS = 1.0

_ACTIVITY_SQL = """
    SELECT pid, state, wait_event_type, wait_event, xact_start, left(query, 200)
      FROM pg_stat_activity
     WHERE datname = current_database()
       AND pid <> pg_backend_pid()
     ORDER BY pid
"""

_started = False
_started_lock = threading.Lock()


def _setting(name, default):
    """Read an int setting from the environment, then ``odoo.conf``."""
    raw = os.environ.get(name.upper())
    if raw is None:
        raw = config.get(name)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _database_name():
    """Best-effort database name, without provoking a registry load.

    ``config['db_name']`` is set on Odoo.sh and in any single-database
    deployment. Otherwise fall back to a registry that is already being built —
    unambiguous only when there is exactly one.
    """
    configured = (config.get("db_name") or "").split(",")[0].strip()
    if configured:
        return configured
    names = list(Registry.registries.keys())
    return names[0] if len(names) == 1 else None


LOAD_ABSENT = "absent"
LOAD_RUNNING = "running"
LOAD_DONE = "done"


def load_state(db_name, registries=None):
    """Is a registry load for ``db_name`` in flight, finished, or not happening?

    Reads the LRU directly — see the module docstring on why calling
    ``Registry(db_name)`` here would deadlock the watchdog itself. The registry
    is published at ``registry.py:124`` before ``load_modules()`` runs and
    ``ready`` flips at ``:144``, so the two together give all three states.

    ``LOAD_ABSENT`` is not "loading hasn't started yet" — it is "there is
    nothing here to watch", and the caller must disarm rather than measure.
    Otherwise any process that imports this addon without building a registry
    for ``db_name`` (a CLI command, a worker pointed at another database, or one
    whose entry the memory-sized LRU has evicted) would look like a stalled load
    against an idle database, and get killed for it.
    """
    registries = Registry.registries if registries is None else registries
    registry = registries.get(db_name)
    if registry is None:
        return LOAD_ABSENT
    return LOAD_DONE if registry.ready else LOAD_RUNNING


def _snapshot(db_name):
    """Return ``(working, fingerprint)`` for the other backends on this database.

    ``working`` means at least one backend is actively executing something that
    is not blocked on a lock, i.e. the load is progressing and must not be
    killed no matter how long it takes. ``fingerprint`` lets us notice progress
    that individual polls happen to miss.

    On any failure (pool exhausted, database gone) returns ``(True, None)`` —
    "assume progress". A watchdog that cannot see must not shoot.
    """
    try:
        with sql_db.db_connect(db_name).cursor() as cr:
            cr.execute(_ACTIVITY_SQL)
            rows = cr.fetchall()
    except Exception:
        _logger.debug("[orc] load watchdog could not read pg_stat_activity", exc_info=True)
        return True, None

    working = any(
        state == "active" and (wait_event_type or "") != "Lock"
        for _pid, state, wait_event_type, _wait_event, _xact_start, _query in rows
    )
    return working, tuple(rows)


def is_progress(sql_working, cpu_delta, cpu_threshold=CPU_PROGRESS_SECONDS):
    """Did anything happen since the last poll?

    Two independent kinds of progress, because either alone would misjudge a
    healthy load: SQL activity misses Python-only work (which would be killed),
    and CPU alone misses a backend patiently executing a long query on the
    server's clock rather than ours.
    """
    return bool(sql_working or cpu_delta >= cpu_threshold)


class StallTracker:
    """Turns a stream of observations into "how long has nothing happened?".

    Kept a plain class with no I/O so the decision logic is unit-testable
    without a database or a wedged Odoo.
    """

    def __init__(self, now):
        self._fingerprint = None
        self._last_progress = now

    def reset(self, now):
        self._fingerprint = None
        self._last_progress = now

    def update(self, now, working, fingerprint):
        if working or fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._last_progress = now
        return now - self._last_progress


def _report(db_name, stalled, rows, abort):
    """Emit the diagnosis to stderr, which is what Odoo.sh captures."""
    header = (
        "[orc] load watchdog: module loading has made no progress for %ds "
        "(database %s). %s"
    ) % (
        stalled,
        db_name,
        "Aborting the process." if abort else "Dumping all thread stacks.",
    )
    _logger.error(header)
    print(header, file=sys.stderr)
    if rows:
        print("[orc] pg_stat_activity (other backends on this database):", file=sys.stderr)
        for row in rows:
            print("  pid=%s state=%s wait=%s/%s xact_start=%s query=%s" % row, file=sys.stderr)
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    sys.stderr.flush()


def _run():
    poll = max(1, _setting("orc_load_watchdog_poll_seconds", POLL_SECONDS))
    dump_after = _setting("orc_load_watchdog_dump_seconds", DUMP_SECONDS)
    abort_after = _setting("orc_load_watchdog_abort_seconds", ABORT_SECONDS)

    tracker = StallTracker(time.monotonic())
    dumps = 0
    absent_polls = 0
    # process_time() is CPU consumed by this process across all its threads.
    previous_cpu = time.process_time()

    while True:
        time.sleep(poll)

        cpu = time.process_time()
        cpu_delta, previous_cpu = cpu - previous_cpu, cpu

        db_name = _database_name()
        if not db_name:
            continue

        state = load_state(db_name)
        if state == LOAD_DONE:
            _logger.debug("[orc] load watchdog: registry ready, standing down")
            return
        if state == LOAD_ABSENT:
            # Nothing to watch. Never accumulate stall against a database whose
            # load we cannot even see, and stand down if it stays that way.
            absent_polls += 1
            if absent_polls >= ABSENT_GIVE_UP_POLLS:
                _logger.debug(
                    "[orc] load watchdog: no registry load for %s, standing down", db_name
                )
                return
            tracker.reset(time.monotonic())
            continue
        absent_polls = 0

        sql_working, fingerprint = _snapshot(db_name)
        working = is_progress(sql_working, cpu_delta)
        stalled = tracker.update(time.monotonic(), working, fingerprint)

        if abort_after and stalled >= abort_after:
            _report(db_name, int(stalled), fingerprint, abort=True)
            # os._exit, not sys.exit: the main thread is wedged, so nothing
            # would unwind. A non-zero exit is what makes the build fail fast.
            os._exit(1)

        if dump_after and stalled >= dump_after * (dumps + 1):
            dumps += 1
            _report(db_name, int(stalled), fingerprint, abort=False)


def start():
    """Start the watchdog once per process. Safe to call repeatedly."""
    global _started
    with _started_lock:
        if _started:
            return
        # Test runs load modules under a cursor the harness controls and
        # deliberately hold transactions open; the stall heuristic does not
        # apply. ORC_LOAD_WATCHDOG_FORCE exists so the watchdog's own
        # end-to-end test can still exercise it.
        if config.get("test_enable") and not os.environ.get("ORC_LOAD_WATCHDOG_FORCE"):
            return
        _started = True
    threading.Thread(target=_run, name="orc.load_watchdog", daemon=True).start()
