from odoo.tests import TransactionCase

from types import SimpleNamespace

from ..load_watchdog import (
    LOAD_ABSENT,
    LOAD_DONE,
    LOAD_RUNNING,
    StallTracker,
    is_progress,
    load_state,
)


def _row(pid, state, wait_event_type=None, query="SELECT 1"):
    """One pg_stat_activity row in the shape _snapshot() produces."""
    return (pid, state, wait_event_type, None, "2026-07-28 15:52:00", query)


def _working(rows):
    """Mirror of the predicate in _snapshot(); see test_working_predicate_*."""
    return any(
        state == "active" and (wait_event_type or "") != "Lock"
        for _pid, state, wait_event_type, _wait_event, _xact_start, _query in rows
    )


class TestLoadWatchdogStallLogic(TransactionCase):
    """The watchdog aborts the process, so its decision rule is the part that
    must not be wrong: killing a healthy-but-slow build would be worse than the
    hang it protects against. Exercised as pure logic — no database, no threads.
    """

    def test_a_long_running_query_is_never_a_stall(self):
        """The failure mode to avoid: a big migration holding one statement open
        for an hour looks identical to a wedge if you only measure elapsed time.
        An `active` backend is progress, however long it takes."""
        tracker = StallTracker(0)
        rows = (_row(11, "active", query="UPDATE res_partner SET x = 1"),)
        stalled = 0
        for now in range(30, 3600, 30):
            stalled = tracker.update(now, _working(rows), rows)
        self.assertEqual(stalled, 0, "an actively executing backend must reset the clock")

    def test_idle_in_transaction_accumulates_stall(self):
        """The 2026-07-28 shape: the loading transaction is open but its thread
        is waiting on Python, so nothing is executing and nothing changes."""
        tracker = StallTracker(0)
        rows = (_row(11, "idle in transaction"),)
        tracker.update(30, _working(rows), rows)  # first sight sets the baseline
        self.assertEqual(tracker.update(630, _working(rows), rows), 600)

    def test_lock_wait_accumulates_stall(self):
        """`active` but blocked on a Lock is not progress — that is the case
        where two connections are deadlocked in a way Postgres cannot see."""
        tracker = StallTracker(0)
        rows = (_row(11, "active", wait_event_type="Lock", query="ALTER TABLE res_users ..."),)
        tracker.update(30, _working(rows), rows)
        self.assertEqual(tracker.update(930, _working(rows), rows), 900)

    def test_changing_fingerprint_resets_even_without_an_active_backend(self):
        """Belt and braces: if polls keep missing the active moment but the
        snapshot keeps changing, work is still happening."""
        tracker = StallTracker(0)
        first = (_row(11, "idle in transaction", query="INSERT INTO a ..."),)
        second = (_row(11, "idle in transaction", query="INSERT INTO b ..."),)
        tracker.update(30, _working(first), first)
        self.assertEqual(tracker.update(60, _working(second), second), 0)

    def test_unreadable_activity_is_treated_as_progress(self):
        """`_snapshot` returns (True, None) when it cannot query — a watchdog
        that cannot see must not shoot."""
        tracker = StallTracker(0)
        tracker.update(30, True, None)
        self.assertEqual(tracker.update(1830, True, None), 0)

    def test_python_only_work_is_progress(self):
        """A migration or hook crunching in Python leaves its connection `idle in
        transaction` with an unchanged snapshot — identical, from Postgres's
        point of view, to a wedge. Burning CPU is what tells them apart, and
        getting this wrong would abort a healthy deployment."""
        tracker = StallTracker(0)
        rows = (_row(11, "idle in transaction"),)
        stalled = 0
        for now in range(30, 3600, 30):
            # ~29 CPU seconds per 30s interval: busy interpreter, no SQL.
            stalled = tracker.update(now, is_progress(_working(rows), 29.0), rows)
        self.assertEqual(stalled, 0, "Python-only work must never be read as a stall")

    def test_no_cpu_and_no_sql_is_a_stall(self):
        """The hangs this exists for — a lock wait, an untimed socket read, a
        sleep — are all parked in the kernel and consume no CPU."""
        tracker = StallTracker(0)
        rows = (_row(11, "idle in transaction"),)
        tracker.update(30, is_progress(_working(rows), 0.01), rows)
        self.assertEqual(tracker.update(930, is_progress(_working(rows), 0.01), rows), 900)

    def test_is_progress_honours_either_signal(self):
        self.assertTrue(is_progress(True, 0.0), "active SQL alone is progress")
        self.assertTrue(is_progress(False, 5.0), "CPU alone is progress")
        self.assertFalse(is_progress(False, 0.0), "neither is a stall")

    def test_working_predicate_ignores_our_own_idle_connection(self):
        """A pool of idle backends plus one worker still counts as working."""
        rows = (
            _row(11, "idle"),
            _row(12, "idle in transaction"),
            _row(13, "active"),
        )
        self.assertTrue(_working(rows))
        self.assertFalse(_working(rows[:2]))

    def test_stall_clock_can_be_reset(self):
        """Used whenever there is no load to watch, so an unwatchable period
        never counts towards the abort."""
        tracker = StallTracker(0)
        rows = (_row(11, "idle in transaction"),)
        tracker.update(30, False, rows)
        self.assertEqual(tracker.update(630, False, rows), 600)
        tracker.reset(630)
        self.assertEqual(tracker.update(660, False, rows), 0)


class TestLoadWatchdogLoadState(TransactionCase):
    """`load_state` decides whether the watchdog is allowed to arm at all.
    Getting `absent` wrong is the dangerous direction: it would let the
    watchdog measure an idle database and eventually kill a healthy process
    that simply never built this registry.
    """

    def test_running_while_the_registry_is_published_but_not_ready(self):
        registries = {"db": SimpleNamespace(ready=False)}
        self.assertEqual(load_state("db", registries), LOAD_RUNNING)

    def test_done_once_ready(self):
        registries = {"db": SimpleNamespace(ready=True)}
        self.assertEqual(load_state("db", registries), LOAD_DONE)

    def test_absent_when_no_registry_exists_for_this_database(self):
        """A CLI command, a worker pointed at another database, or an entry the
        memory-sized LRU evicted. Nothing to watch — and crucially not a stall."""
        self.assertEqual(load_state("db", {}), LOAD_ABSENT)
        self.assertEqual(load_state("db", {"other": SimpleNamespace(ready=False)}), LOAD_ABSENT)
