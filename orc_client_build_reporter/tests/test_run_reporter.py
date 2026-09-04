"""End-to-end test of the reporter's runtime: mocks the Odoo registry
+ cursor + requests.post so we exercise the full path through
``_run_reporter`` without any DB write or HTTP call escaping.

Inherits ``TransactionCase`` because the reporter reads/writes
``ir.config_parameter`` and we want a savepoint around each test.

``_run_reporter`` takes its webhook base from the caller — the hook
resolves it so a process with none configured never spawns a thread.
``self._run()`` below mirrors that, calling ``_resolve_webhook_base``
exactly like the hook does, so the ICP-driven tests keep exercising
the real resolution path.

The claim/POST/stamp sequence runs inside one transaction guarded by a
transaction-scoped advisory lock. `_FakeRegistry` hands back the test's
own cursor, so real cross-connection contention is not reachable from
`TransactionCase` — the loser branch is covered by patching
``_try_lock`` instead. What that leaves untested is Postgres' own
mutual exclusion, which is not ours to test.
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
    REPO = "opsway/pg_group"
    DBNAME = "pg-group-stage-25407779"
    BUILD_ID = "25407779"
    BRANCH_SLUG = "pg-group-stage"
    WEBHOOK_BASE = "https://orc.test/webhook/odoo-sh/build-ready"

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        # Use ICP override instead of monkey-patching the module-level
        # constant — ICP path is the documented runtime-override
        # mechanism and exercising it here doubles as coverage.
        self.ICP.set_param(reporter._PARAM_WEBHOOK_BASE, self.WEBHOOK_BASE)
        self.ICP.set_param(reporter._PARAM_LAST_REPORT, False)
        self.env.cache.invalidate()
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

    def _stack_patches(self, *, sha=_UNSET, repo=_UNSET, config_overrides=None):
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
                reporter, "get_repo_from_git",
                return_value=self.REPO if repo is _UNSET else repo,
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

    def _run(self, dbname=None, **overrides):
        """Drive one reporter run the way `_register_hook` does."""
        self.env.cache.invalidate()
        webhook_base = overrides.get(
            "webhook_base", reporter._resolve_webhook_base(self.env),
        )
        reporter._run_reporter(dbname or self.DBNAME, webhook_base)

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
            self._run()

        m_post.assert_called_once()
        args, kwargs = m_post.call_args
        # URL: {webhook_base}/{sha}   (no org_id — repo lives in body)
        self.assertEqual(args[0], f"{self.WEBHOOK_BASE}/{self.SHA}")

        body = kwargs["json"]
        self.assertEqual(body["build_id"], self.BUILD_ID)
        self.assertEqual(body["branch_slug"], self.BRANCH_SLUG)
        self.assertEqual(body["stage"], "dev")  # default when ODOO_STAGE unset
        self.assertEqual(body["repo"], self.REPO)
        self.assertEqual(
            body["build_url"],
            f"https://{self.BRANCH_SLUG}-{self.BUILD_ID}.dev.odoo.com",
        )

        headers = kwargs["headers"]
        self.assertIn("User-Agent", headers)
        # No Authorization header — that was the v1 GH-PAT path.
        self.assertNotIn("Authorization", headers)
        self.assertEqual(kwargs["timeout"], 10)

        self.env.cache.invalidate()
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
            self._run()

        body = m_post.call_args.kwargs["json"]
        self.assertEqual(body["build_id"], "32258372")
        self.assertEqual(body["branch_slug"], "pg-group-feature-pg-460-ai")
        self.assertEqual(body["stage"], "dev")
        self.assertEqual(body["repo"], self.REPO)
        self.assertEqual(
            body["build_url"],
            "https://pg-group-feature-pg-460-ai-32258372.dev.odoo.com",
        )

    # --- Skip conditions ---------------------------------------------------

    def _run_and_assert_no_post(self, patches, dbname=None, **overrides):
        self._start(patches)
        with mock.patch.object(reporter.requests, "post") as m_post:
            self._run(dbname, **overrides)
        m_post.assert_not_called()

    def test_skip_when_test_enable_set(self):
        self._run_and_assert_no_post(
            self._stack_patches(config_overrides={"test_enable": True}),
        )

    def test_skip_when_test_file_set(self):
        self._run_and_assert_no_post(
            self._stack_patches(config_overrides={"test_file": "x.py"}),
        )

    def test_skip_when_stop_after_init_set(self):
        """The build-phase guard. `--stop-after-init` means the process
        exits the moment the registry is loaded, taking the connection
        pool with it — a report attempted from there can only lose the
        race, sometimes loudly enough to red the whole build."""
        self._run_and_assert_no_post(
            self._stack_patches(config_overrides={"stop_after_init": True}),
        )

    def test_skip_when_dbname_has_no_build_id(self):
        self._run_and_assert_no_post(self._stack_patches(), dbname="local-dev")

    def test_skip_when_webhook_base_missing(self):
        self.ICP.set_param(reporter._PARAM_WEBHOOK_BASE, False)
        self.env.cache.invalidate()
        with mock.patch.object(reporter, "WEBHOOK_BASE", ""):
            self._run_and_assert_no_post(self._stack_patches())

    def test_skip_when_sha_unknown(self):
        self._run_and_assert_no_post(self._stack_patches(sha=None))

    def test_skip_when_repo_unknown(self):
        self._run_and_assert_no_post(self._stack_patches(repo=None))

    # --- Debounce ----------------------------------------------------------

    def test_skip_when_same_report_key_already_seen(self):
        self.ICP.set_param(
            reporter._PARAM_LAST_REPORT,
            f"{self.SHA}:{self.BUILD_ID}:dev",
        )
        self.env.cache.invalidate()
        self._run_and_assert_no_post(self._stack_patches())

    def test_same_sha_new_build_id_reposts(self):
        """A rebuild on the same commit gets a fresh build_id; the
        debounce key includes build_id so the new build_id re-posts."""
        self.ICP.set_param(
            reporter._PARAM_LAST_REPORT,
            f"{self.SHA}:99999999:dev",
        )
        self.env.cache.invalidate()
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter.requests, "post", return_value=self._fake_response(),
        ) as m_post:
            self._run()
        m_post.assert_called_once()

    def test_same_sha_new_stage_reposts(self):
        """Same SHA promoted dev → staging should re-post (different
        stage = different row in Workplace's table)."""
        self.ICP.set_param(
            reporter._PARAM_LAST_REPORT,
            f"{self.SHA}:{self.BUILD_ID}:dev",
        )
        self.env.cache.invalidate()
        os.environ["ODOO_STAGE"] = "staging"
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter.requests, "post", return_value=self._fake_response(),
        ) as m_post:
            self._run()
        m_post.assert_called_once()
        self.assertEqual(m_post.call_args.kwargs["json"]["stage"], "staging")

    def test_loser_of_the_lock_does_not_post(self):
        """Two workers loading the same registry derive the same tuple.
        Only the lock holder may POST — otherwise the stale read each of
        them did would produce one duplicate report per worker, which is
        the "debounce across Odoo workers" promise not being kept."""
        self._start(self._stack_patches())
        with mock.patch.object(reporter, "_try_lock", return_value=False), \
                mock.patch.object(reporter.requests, "post") as m_post:
            self._run()
        m_post.assert_not_called()
        self.env.cache.invalidate()
        self.assertFalse(
            self.ICP.get_param(reporter._PARAM_LAST_REPORT),
            "a worker that lost the lock must not stamp either",
        )

    def test_winner_of_the_lock_posts_and_stamps(self):
        """The complement: with the lock granted, the same run posts."""
        self._start(self._stack_patches())
        with mock.patch.object(reporter, "_try_lock", return_value=True), \
                mock.patch.object(
                    reporter.requests, "post",
                    return_value=self._fake_response(),
                ) as m_post:
            self._run()
        m_post.assert_called_once()
        self.env.cache.invalidate()
        self.assertEqual(
            self.ICP.get_param(reporter._PARAM_LAST_REPORT),
            f"{self.SHA}:{self.BUILD_ID}:dev",
        )

    def test_debounce_is_re_read_under_the_lock(self):
        """The check that must not be a snapshot taken before the lock:
        a worker queued behind the winner has to see what the winner
        stamped, not what it read on its way in."""
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter.requests, "post", return_value=self._fake_response(),
        ) as m_post:
            self._run()
            m_post.assert_called_once()
            # Second worker, same tuple, lock now free again.
            self._run()
        self.assertEqual(
            m_post.call_count, 1,
            "the second worker must observe the first worker's stamp",
        )

    def test_consecutive_run_skipped_unless_icp_cleared(self):
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter.requests, "post", return_value=self._fake_response(),
        ) as m_post:
            self._run()
            self._run()

            m_post.assert_called_once()
            self.env.cache.invalidate()
            self.assertEqual(
                self.ICP.get_param(reporter._PARAM_LAST_REPORT),
                f"{self.SHA}:{self.BUILD_ID}:dev",
            )

            self.ICP.set_param(reporter._PARAM_LAST_REPORT, False)
            self.env.cache.invalidate()
            self._run()

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
                self._run()
            except Exception as e:
                self.fail(f"_run_reporter raised: {e!r}")

    def test_never_raises_on_internal_error(self):
        self._start(self._stack_patches())
        with mock.patch.object(
            reporter, "get_commit_sha", side_effect=RuntimeError("boom"),
        ):
            try:
                self._run()
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
                self._run()
            except Exception as e:
                self.fail(f"_run_reporter raised: {e!r}")

    def test_failed_post_does_not_stamp_so_next_run_retries(self):
        """A webhook failure must NOT persist the debounce key: the next
        registry restart re-POSTs the same {sha,build_id,stage}. Stamping
        before the POST (the pre-fix bug) committed the key on cursor exit,
        so any transient timeout / non-2xx silently suppressed the
        advertised retry-on-next-restart path."""
        self._start(self._stack_patches())
        bad = mock.Mock()
        bad.raise_for_status = mock.Mock(
            side_effect=Exception("503 ServiceUnavailable"),
        )
        with mock.patch.object(
            reporter.requests, "post",
            side_effect=[bad, self._fake_response()],
        ) as m_post:
            self._run()                           # fails → must not stamp
            self.env.cache.invalidate()
            self.assertFalse(
                self.ICP.get_param(reporter._PARAM_LAST_REPORT),
                "debounce key must not be stamped after a failed POST",
            )
            self._run()                           # retries

        self.assertEqual(
            m_post.call_count, 2,
            "the run after a failed POST must retry, not skip",
        )
        self.env.cache.invalidate()
        self.assertEqual(
            self.ICP.get_param(reporter._PARAM_LAST_REPORT),
            f"{self.SHA}:{self.BUILD_ID}:dev",
        )
