"""Tests for the two pieces that keep the reporter off the connection
pool during shutdown: `_skip_reason` and the `_register_hook` wiring.

Background — the failure these guard against
--------------------------------------------

`ThreadedServer.run()` calls `preload_registries()` (where
`_register_hook` fires and this addon starts its daemon thread) and
then, under `--stop-after-init`, `self.stop()` immediately. `stop()`
joins only *non-daemon* threads before `sql_db.close_all()`, so the
reporter thread's freshly borrowed cursor is closed mid-query.
`sql_db.execute` logs that at ERROR one frame below this addon's
try/except — unsuppressable — and Odoo.sh reds the entire build over a
single ERROR line in `update.log`, even when the build succeeded.
"""
from unittest import mock

from odoo.tests import tagged
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.orc_client_build_reporter.models import build_reporter as reporter


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestSkipReason(BaseCase):

    def _reason(self, **overrides):
        with mock.patch.object(reporter, "config", overrides):
            return reporter._skip_reason()

    def test_runs_when_nothing_set(self):
        self.assertIsNone(self._reason())

    def test_test_enable(self):
        self.assertEqual(self._reason(test_enable=True), reporter._SKIP_TEST_MODE)

    def test_test_file(self):
        self.assertEqual(self._reason(test_file="x.py"), reporter._SKIP_TEST_MODE)

    def test_stop_after_init(self):
        self.assertEqual(
            self._reason(stop_after_init=True),
            reporter._SKIP_STOP_AFTER_INIT,
        )

    def test_test_mode_wins_over_stop_after_init(self):
        """Test runs normally pass both flags. Test mode must win, since
        it is the reason the caller keeps silent — an INFO line on every
        CI run is noise nobody asked for."""
        self.assertEqual(
            self._reason(test_enable=True, stop_after_init=True),
            reporter._SKIP_TEST_MODE,
        )


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestReadConfig(TransactionCase):
    """`_read_config` is what moved the pre-POST DB reads out of the
    thread and onto the caller's cursor."""

    WEBHOOK_BASE = "https://orc.test/webhook/odoo-sh/build-ready"

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()

    def _read(self):
        self.env.invalidate_all()
        return reporter._read_config(self.env)

    def test_icp_overrides_in_source_constant(self):
        self.ICP.set_param(reporter._PARAM_WEBHOOK_BASE, self.WEBHOOK_BASE)
        with mock.patch.object(reporter, "WEBHOOK_BASE", "https://in-source/x"):
            self.assertEqual(self._read()[0], self.WEBHOOK_BASE)

    def test_falls_back_to_in_source_constant(self):
        self.ICP.set_param(reporter._PARAM_WEBHOOK_BASE, False)
        with mock.patch.object(reporter, "WEBHOOK_BASE", "https://in-source/x"):
            self.assertEqual(self._read()[0], "https://in-source/x")

    def test_none_when_neither_set(self):
        self.ICP.set_param(reporter._PARAM_WEBHOOK_BASE, False)
        with mock.patch.object(reporter, "WEBHOOK_BASE", ""):
            self.assertIsNone(self._read()[0])

    def test_returns_stored_debounce_key(self):
        self.ICP.set_param(reporter._PARAM_LAST_REPORT, "sha:1:dev")
        self.assertEqual(self._read()[1], "sha:1:dev")

    def test_debounce_key_none_when_unset(self):
        self.ICP.set_param(reporter._PARAM_LAST_REPORT, False)
        self.assertIsNone(self._read()[1])


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestRegisterHook(TransactionCase):
    """The hook decides whether a thread is spawned at all, and must
    never let an exception escape into the module-loading transaction."""

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.ICP.set_param(
            reporter._PARAM_WEBHOOK_BASE,
            "https://orc.test/webhook/odoo-sh/build-ready",
        )
        self.env.invalidate_all()
        self.modules = self.env["ir.module.module"]

    def _fire(self, **config_overrides):
        """Run the hook with the thread constructor mocked out, so no
        real reporter thread escapes the test."""
        with mock.patch.object(reporter, "config", config_overrides), \
                mock.patch.object(reporter.threading, "Thread") as m_thread:
            self.modules._register_hook()
        return m_thread

    def test_spawns_thread_when_serving(self):
        m_thread = self._fire()
        m_thread.assert_called_once()
        m_thread.return_value.start.assert_called_once()

    def test_thread_receives_config_read_on_the_hook_cursor(self):
        """The whole point of the change: the thread is handed both ICP
        values, so it performs no DB access before the POST."""
        self.ICP.set_param(reporter._PARAM_LAST_REPORT, "sha:1:dev")
        self.env.invalidate_all()
        m_thread = self._fire()
        args = m_thread.call_args.kwargs["args"]
        self.assertEqual(
            args,
            (
                self.env.cr.dbname,
                "https://orc.test/webhook/odoo-sh/build-ready",
                "sha:1:dev",
            ),
        )

    def test_thread_is_daemon(self):
        """Daemon is deliberate — a non-daemon thread parked in the 10s
        POST would block interpreter exit. It is *because* it is a
        daemon that the `--stop-after-init` guard is needed."""
        self.assertTrue(self._fire().call_args.kwargs["daemon"])

    def test_no_thread_under_stop_after_init(self):
        """The regression this whole change exists to prevent."""
        self._fire(stop_after_init=True).assert_not_called()

    def test_no_thread_in_test_mode(self):
        self._fire(test_enable=True).assert_not_called()

    def test_no_thread_without_webhook_base(self):
        self.ICP.set_param(reporter._PARAM_WEBHOOK_BASE, False)
        self.env.invalidate_all()
        with mock.patch.object(reporter, "WEBHOOK_BASE", ""):
            self._fire().assert_not_called()

    def test_never_raises_into_the_loading_transaction(self):
        """The hook runs inside loading.py STEP 9; an exception escaping
        it aborts the registry load — turning a cosmetic problem into a
        genuinely broken build."""
        with mock.patch.object(reporter, "config", {}), \
                mock.patch.object(
                    reporter, "_read_config", side_effect=RuntimeError("boom"),
                ):
            try:
                self.modules._register_hook()
            except Exception as e:
                self.fail(f"_register_hook raised: {e!r}")
