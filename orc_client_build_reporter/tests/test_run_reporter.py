"""End-to-end test of the reporter's runtime: mocks the Odoo registry
+ cursor + requests.post so we exercise the full path through
``_run_reporter`` without any DB write or HTTP call escaping.

Inherits ``TransactionCase`` because the reporter reads/writes
``ir.config_parameter`` and we want a savepoint around each test.
"""
import os
from unittest import mock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.orc_client_build_reporter.models import build_reporter as reporter

_UNSET = object()


class _FakeCursorCM:
    """Yields the test transaction's cursor; never commits or rolls back so
    the surrounding TransactionCase keeps full control of isolation."""

    def __init__(self, cr):
        self.cr = cr

    def __enter__(self):
        return self.cr

    def __exit__(self, *_):
        return False


class _FakeRegistry:
    def __init__(self, cr):
        self.cr = cr

    def cursor(self):
        return _FakeCursorCM(self.cr)


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestRunReporter(TransactionCase):
    SHA = "b" * 40
    DBNAME = "pg-group-stage-25407779"
    BUILD_ID = "25407779"
    BRANCH_SLUG = "pg-group-stage"
    ORG_ID = "11111111-2222-3333-4444-555555555555"
    WEBHOOK_BASE = "https://orc.test/webhook/odoo-sh/build-ready"

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        # Use ICP overrides instead of monkey-patching module-level
        # constants — ICP path is the documented runtime-override
        # mechanism and exercising it here doubles as coverage.
        self.ICP.set_param(reporter._PARAM_ORG_ID, self.ORG_ID)
        self.ICP.set_param(reporter._PARAM_WEBHOOK_BASE, self.WEBHOOK_BASE)
        self.ICP.set_param(reporter._PARAM_LAST_REPORT, False)
        self.env.invalidate_all()
        # Each test starts with no Odoo.sh env hints; individual tests opt in.
        self._saved_env = {
            k: os.environ.pop(k, None)
            for k in ("ODOO_BUILD_URL", "ODOO_STAGE")
        }
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _stack_patches(self, *, sha=_UNSET, config_overrides=None):
        return [
            mock.patch.object(
                reporter, "config",
                config_overrides if config_overrides is not None else {},
            ),
            mock.patch.object(
                reporter, "get_commit_sha",
                return_value=self.SHA if sha is _UNSET else sha,
            ),
            mock.patch.object(
                reporter, "Registry",
                return_value=_FakeRegistry(self.env.cr),
            ),
        ]

    def _start(self, patches):
        for p in patches:
            p.start()
        self.addCleanup(self._stop_all, patches)

    @staticmethod
    def _stop_all(patches):
        for p in patches:
            p.stop()

    @staticmethod
    def _fake_response(status_code=200):
        r = mock.Mock()
        r.raise_for_status = mock.Mock()
        r.status_code = status_code
        r.text = '{"ok": true}'
        return r

    # --- Happy path: dbname-derived (no ODOO_BUILD_URL env var) -----------

    def test_happy_path_posts_correct_url_and_body(self):
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter.requests, "post", return_value=self._fake_response(),
        ) as m_post:
            reporter._run_reporter(self.DBNAME)

        m_post.assert_called_once()
        args, kwargs = m_post.call_args
        self.assertEqual(
            args[0],
            f"{self.WEBHOOK_BASE}/{self.ORG_ID}/{self.SHA}",
        )
        body = kwargs["json"]
        self.assertEqual(body["build_id"], self.BUILD_ID)
        self.assertEqual(body["branch_slug"], self.BRANCH_SLUG)
        self.assertEqual(body["stage"], "dev")  # default when ODOO_STAGE unset
        self.assertEqual(
            body["build_url"],
            f"https://{self.BRANCH_SLUG}-{self.BUILD_ID}.dev.odoo.com",
        )

        headers = kwargs["headers"]
        self.assertIn("User-Agent", headers)
        # No Authorization header — that was the v1 GH-PAT path.
        self.assertNotIn("Authorization", headers)
        self.assertEqual(kwargs["timeout"], 10)

        self.env.invalidate_all()
        self.assertEqual(
            self.ICP.get_param(reporter._PARAM_LAST_REPORT),
            f"{self.SHA}:{self.BUILD_ID}:dev",
        )

    # --- Happy path: ODOO_BUILD_URL drives the parse -----------------------

    def test_uses_odoo_build_url_when_set(self):
        os.environ["ODOO_BUILD_URL"] = (
            "https://pg-group-feature-pg-460-ai-32258372.dev.odoo.com"
        )
        os.environ["ODOO_STAGE"] = "dev"
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter.requests, "post", return_value=self._fake_response(),
        ) as m_post:
            reporter._run_reporter(self.DBNAME)

        body = m_post.call_args.kwargs["json"]
        self.assertEqual(body["build_id"], "32258372")
        self.assertEqual(body["branch_slug"], "pg-group-feature-pg-460-ai")
        self.assertEqual(body["stage"], "dev")
        self.assertEqual(
            body["build_url"],
            "https://pg-group-feature-pg-460-ai-32258372.dev.odoo.com",
        )

    # --- Skip conditions ---------------------------------------------------

    def _run_and_assert_no_post(self, dbname, patches):
        self._start(patches)
        with mock.patch.object(reporter.requests, "post") as m_post:
            reporter._run_reporter(dbname)
        m_post.assert_not_called()

    def test_skip_when_test_enable_set(self):
        self._run_and_assert_no_post(
            self.DBNAME,
            self._stack_patches(config_overrides={"test_enable": True}),
        )

    def test_skip_when_test_file_set(self):
        self._run_and_assert_no_post(
            self.DBNAME,
            self._stack_patches(config_overrides={"test_file": "x.py"}),
        )

    def test_skip_when_dbname_has_no_build_id(self):
        self._run_and_assert_no_post("local-dev", self._stack_patches())

    def test_skip_when_org_id_missing(self):
        self.ICP.set_param(reporter._PARAM_ORG_ID, False)
        self.env.invalidate_all()
        # Ensure the module-level fallback is empty too so a real
        # ORG_ID baked in by a fork doesn't rescue this test.
        with mock.patch.object(reporter, "ORG_ID", ""):
            self._run_and_assert_no_post(self.DBNAME, self._stack_patches())

    def test_skip_when_webhook_base_missing(self):
        self.ICP.set_param(reporter._PARAM_WEBHOOK_BASE, False)
        self.env.invalidate_all()
        with mock.patch.object(reporter, "WEBHOOK_BASE", ""):
            self._run_and_assert_no_post(self.DBNAME, self._stack_patches())

    def test_skip_when_sha_unknown(self):
        self._run_and_assert_no_post(
            self.DBNAME, self._stack_patches(sha=None),
        )

    # --- Debounce ----------------------------------------------------------

    def test_skip_when_same_report_key_already_seen(self):
        self.ICP.set_param(
            reporter._PARAM_LAST_REPORT,
            f"{self.SHA}:{self.BUILD_ID}:dev",
        )
        self.env.invalidate_all()
        self._run_and_assert_no_post(self.DBNAME, self._stack_patches())

    def test_same_sha_new_build_id_reposts(self):
        """A rebuild on the same commit gets a fresh build_id; the
        debounce key includes build_id so the new build_id re-posts."""
        self.ICP.set_param(
            reporter._PARAM_LAST_REPORT,
            f"{self.SHA}:99999999:dev",
        )
        self.env.invalidate_all()
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter.requests, "post", return_value=self._fake_response(),
        ) as m_post:
            reporter._run_reporter(self.DBNAME)
        m_post.assert_called_once()

    def test_same_sha_new_stage_reposts(self):
        """Same SHA promoted dev → staging should re-post (different
        stage = different row in Workplace's table)."""
        self.ICP.set_param(
            reporter._PARAM_LAST_REPORT,
            f"{self.SHA}:{self.BUILD_ID}:dev",
        )
        self.env.invalidate_all()
        os.environ["ODOO_STAGE"] = "staging"
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter.requests, "post", return_value=self._fake_response(),
        ) as m_post:
            reporter._run_reporter(self.DBNAME)
        m_post.assert_called_once()
        self.assertEqual(m_post.call_args.kwargs["json"]["stage"], "staging")

    def test_consecutive_run_skipped_unless_icp_cleared(self):
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter.requests, "post", return_value=self._fake_response(),
        ) as m_post:
            reporter._run_reporter(self.DBNAME)
            reporter._run_reporter(self.DBNAME)

            m_post.assert_called_once()
            self.env.invalidate_all()
            self.assertEqual(
                self.ICP.get_param(reporter._PARAM_LAST_REPORT),
                f"{self.SHA}:{self.BUILD_ID}:dev",
            )

            self.ICP.set_param(reporter._PARAM_LAST_REPORT, False)
            self.env.invalidate_all()
            reporter._run_reporter(self.DBNAME)

        self.assertEqual(
            m_post.call_count, 2,
            "third call after clearing the ICP should POST again",
        )

    # --- Resilience --------------------------------------------------------

    def test_never_raises_on_post_failure(self):
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter.requests, "post", side_effect=RuntimeError("kaboom"),
        ):
            try:
                reporter._run_reporter(self.DBNAME)
            except Exception as e:
                self.fail(f"_run_reporter raised: {e!r}")

    def test_never_raises_on_internal_error(self):
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter, "get_commit_sha", side_effect=RuntimeError("boom"),
        ):
            try:
                reporter._run_reporter(self.DBNAME)
            except Exception as e:
                self.fail(f"_run_reporter raised: {e!r}")

    def test_never_raises_on_http_error_status(self):
        self._start(self._stack_patches())
        bad = mock.Mock()
        bad.raise_for_status = mock.Mock(
            side_effect=Exception("403 Forbidden"),
        )
        with mock.patch.object(reporter.requests, "post", return_value=bad):
            try:
                reporter._run_reporter(self.DBNAME)
            except Exception as e:
                self.fail(f"_run_reporter raised: {e!r}")
