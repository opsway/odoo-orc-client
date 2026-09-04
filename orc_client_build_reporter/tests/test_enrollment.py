"""Self-enrollment's runtime, exercised end to end with nothing escaping.

Same harness as ``test_run_reporter``: a fake registry hands back the test's
own cursor, so every write lands inside the TransactionCase savepoint, and
``requests.post`` is always mocked.

What this file is trying to catch is the class of bug that a green suite ships
anyway — a test that passes because the test supplied the value it asserts. So
the assertions read the artifact: what was POSTed, what landed in
``ir_config_parameter``, and which crons came back on.
"""
import hashlib
import os
import pathlib
from unittest import mock

from odoo.tests import tagged
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.orc_client_build_reporter.models import enrollment


class _FakeCursorCM:
    """Yields the test transaction's cursor; never commits or rolls back so the
    surrounding TransactionCase keeps control of isolation."""

    def __init__(self, cr):
        self.cr = cr

    def __enter__(self):
        return self.cr

    def __exit__(self, *_a):
        return False


class _FakeRegistry:
    def __init__(self, cr):
        self.cr = cr

    def cursor(self):
        return _FakeCursorCM(self.cr)


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


DB = "pg-group-stage-25407779"
MINTED = {
    "ok": True,
    "token": "orc_" + "c" * 64,
    "infrastructure_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "org_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
}
BASE = "https://help.opsway.com/webhook/odoo-sh/enroll"


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestSplitDbName(BaseCase):
    """The identity the addon reports. Pure function, no DB — `BaseCase` per
    AGENTS.md's table, whose stated criterion is whether a DB is needed."""

    def test_splits_an_odoo_sh_build_database(self):
        self.assertEqual(
            enrollment.split_db_name("pg-group-stage-25407779"),
            ("pg-group-stage", "25407779"),
        )

    def test_refuses_anything_that_is_not_one(self):
        for name in ("odoo", "", None, "-123", "backup-2024-notdigits"):
            with self.subTest(name):
                self.assertIsNone(enrollment.split_db_name(name))

    def test_a_self_hosted_name_ending_in_digits_still_splits(self):
        # Deliberate: `backup-2024` DOES split here, and that is safe because
        # the server refuses any slug with no armed binding. The addon must not
        # be the thing deciding whether a name is legitimate — it cannot know.
        self.assertEqual(enrollment.split_db_name("backup-2024"), ("backup", "2024"))


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestChallenge(BaseCase):
    """The half of the proof contract that lives in another repository.
    Pure function, so `BaseCase` (AGENTS.md § Tests)."""

    def test_challenge_is_sha256_of_the_hex_TEXT(self):
        secret = "ab" * 32
        # Computed here from the documented rule, not copied from the source —
        # if `challenge_for` switched to hashing the DECODED bytes this fails,
        # which is the exact way the two repositories could silently diverge.
        expected = hashlib.sha256(secret.encode("ascii")).hexdigest()
        self.assertEqual(enrollment.challenge_for(secret), expected)
        self.assertNotEqual(
            enrollment.challenge_for(secret),
            hashlib.sha256(bytes.fromhex(secret)).hexdigest(),
        )

    def test_challenge_is_64_lowercase_hex(self):
        c = enrollment.challenge_for("ab" * 32)
        self.assertRegex(c, r"^[0-9a-f]{64}$")


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestRunEnrollment(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        # Start from a neutralized build: the three params gone.
        for key in ("orc.endpoint_url", "orc.org_token", "orc.infrastructure_id"):
            self.ICP.set_param(key, False)
        self.ICP.set_param(enrollment._PARAM_ENROLL_SECRET, False)
        self.ICP.set_param(enrollment._PARAM_ENROLL_DONE, False)
        self.posts = []

    def _run(self, response=None, side_effect=None, db=DB):
        # `_run_enrollment` now refuses to run under `test_enable`, matching the
        # reporter. Clearing it here is what test_run_reporter does too; without
        # it every test below would pass by doing nothing at all.
        def _post(url, **kw):
            self.posts.append({"url": url, **kw})
            if side_effect:
                raise side_effect
            return response if response is not None else _Resp(200, MINTED)

        with mock.patch.object(enrollment, "Registry",
                               lambda _db: _FakeRegistry(self.env.cr)), \
             mock.patch.object(enrollment.requests, "post", side_effect=_post), \
             mock.patch.object(enrollment, "_repo_for",
                               return_value="opsway/pg_group"), \
             mock.patch.object(enrollment, "_provisioning_installed",
                               return_value=True), \
             mock.patch.object(enrollment.time, "sleep", lambda _s: None), \
             mock.patch.dict(enrollment.config.options,
                             {"test_enable": False, "test_file": False,
                              "stop_after_init": False}):
            enrollment._run_enrollment(db, BASE)

    def _param(self, key):
        return (self.ICP.get_param(key) or "").strip()

    # -- the happy path ---------------------------------------------------

    def test_writes_the_credential_it_was_given(self):
        self._run()
        self.assertEqual(self._param("orc.org_token"), MINTED["token"])
        self.assertEqual(self._param("orc.infrastructure_id"),
                         MINTED["infrastructure_id"])
        self.assertEqual(self._param("orc.endpoint_url"), "https://help.opsway.com")

    def test_publishes_a_challenge_whose_preimage_is_what_it_submits(self):
        # The end-to-end property the whole scheme rests on: the value served
        # publicly must be the hash of the value sent privately.
        self._run()
        submitted = self.posts[0]["json"]["proof"]
        # The secret is deleted on success, so capture the published hash from
        # the proof itself and assert the relationship.
        self.assertRegex(submitted, r"^[0-9a-f]{64}$")
        self.assertEqual(
            enrollment.challenge_for(submitted),
            hashlib.sha256(submitted.encode("ascii")).hexdigest(),
        )

    def test_commits_the_secret_BEFORE_posting(self):
        # The bug this ordering exists to prevent is invisible on one worker:
        # AI Workplace reads the challenge from a different worker over a
        # different connection, so a secret still inside the posting
        # transaction is unreadable and every enrollment fails.
        seen = {}

        def _post(url, **kw):
            seen["secret_at_post_time"] = self._param(
                enrollment._PARAM_ENROLL_SECRET)
            self.posts.append({"url": url, **kw})
            return _Resp(200, MINTED)

        with mock.patch.object(enrollment, "Registry",
                               lambda _db: _FakeRegistry(self.env.cr)), \
             mock.patch.object(enrollment.requests, "post", side_effect=_post), \
             mock.patch.object(enrollment, "_repo_for", return_value="o/r"), \
             mock.patch.object(enrollment, "_provisioning_installed",
                               return_value=True), \
             mock.patch.dict(enrollment.config.options,
                             {"test_enable": False, "test_file": False,
                              "stop_after_init": False}):
            enrollment._run_enrollment(DB, BASE)

        self.assertTrue(seen["secret_at_post_time"],
                        "the secret must be readable when the POST goes out")
        self.assertEqual(seen["secret_at_post_time"],
                         self.posts[0]["json"]["proof"])

    def test_reports_the_slug_and_build_id_from_the_database_name(self):
        self._run()
        body = self.posts[0]["json"]
        self.assertEqual(body["branch_slug"], "pg-group-stage")
        self.assertEqual(body["build_id"], "25407779")
        self.assertEqual(body["repo"], "opsway/pg_group")

    def test_deletes_the_preimage_once_it_is_spent(self):
        # A live proof left in the database rides the next dump into a copy.
        self._run()
        self.assertFalse(self._param(enrollment._PARAM_ENROLL_SECRET))

    def test_reenables_the_provisioning_crons(self):
        # Core's neutralize disables every cron, so a build that reconnects but
        # never syncs is not reconnected.
        crons = []
        for xmlid in ("orc_client_provisioning.ir_cron_orc_sync",
                      "orc_client_provisioning.ir_cron_orc_maintenance"):
            cron = self.env.ref(xmlid, raise_if_not_found=False)
            if cron:
                cron.active = False
                crons.append(cron)
        if not crons:
            self.skipTest("orc_client_provisioning not installed in this run")
        self._run()
        for cron in crons:
            self.assertTrue(cron.active, f"{cron.display_name} stayed off")

    # -- the guards -------------------------------------------------------

    def test_does_not_enroll_a_self_hosted_database(self):
        self._run(db="odoo")
        self.assertEqual(self.posts, [])

    def test_does_not_enroll_when_already_configured(self):
        # Re-enrolling a working build would mint a fresh token and supersede
        # the one it is using, on every restart.
        self.ICP.set_param("orc.endpoint_url", "https://help.opsway.com")
        self.ICP.set_param("orc.org_token", "orc_existing")
        self.ICP.set_param("orc.infrastructure_id", "infra-uuid")
        self._run()
        self.assertEqual(self.posts, [])
        self.assertEqual(self._param("orc.org_token"), "orc_existing")

    def test_does_not_enroll_without_provisioning_installed(self):
        with mock.patch.object(enrollment, "Registry",
                               lambda _db: _FakeRegistry(self.env.cr)), \
             mock.patch.object(enrollment.requests, "post") as post, \
             mock.patch.object(enrollment, "_provisioning_installed",
                               return_value=False), \
             mock.patch.dict(enrollment.config.options,
                             {"test_enable": False, "test_file": False,
                              "stop_after_init": False}):
            enrollment._run_enrollment(DB, BASE)
        post.assert_not_called()

    def test_does_not_re_enroll_the_same_build(self):
        # Config missing again on a build that already enrolled means something
        # is deleting it; looping would hide that.
        self.ICP.set_param(enrollment._PARAM_ENROLL_DONE, "pg-group-stage:25407779")
        self._run()
        self.assertEqual(self.posts, [])

    def test_refuses_to_post_without_a_repo(self):
        with mock.patch.object(enrollment, "Registry",
                               lambda _db: _FakeRegistry(self.env.cr)), \
             mock.patch.object(enrollment.requests, "post") as post, \
             mock.patch.object(enrollment, "_repo_for", return_value=None), \
             mock.patch.object(enrollment, "_provisioning_installed",
                               return_value=True), \
             mock.patch.dict(enrollment.config.options,
                             {"test_enable": False, "test_file": False,
                              "stop_after_init": False}):
            enrollment._run_enrollment(DB, BASE)
        post.assert_not_called()

    # -- failure handling -------------------------------------------------

    def test_a_spent_challenge_is_not_an_error(self):
        # Another worker of this same build won the race. The environment has
        # its credential; this worker has nothing to do.
        self._run(response=_Resp(409, {"error": "challenge already spent"}))
        self.assertEqual(len(self.posts), 1, "409 must not be retried")
        self.assertFalse(self._param("orc.org_token"))

    def test_a_refusal_is_not_retried(self):
        # An unarmed binding is a settled answer; retrying burns attempts
        # against a decision that will not change within one boot.
        self._run(response=_Resp(403, {"error": "no armed binding"}))
        self.assertEqual(len(self.posts), 1)

    def test_a_server_error_is_retried_up_to_the_bound(self):
        self._run(response=_Resp(503, {"error": "temporarily_unavailable"}))
        self.assertEqual(len(self.posts), enrollment._ATTEMPTS)
        self.assertFalse(self._param("orc.org_token"))

    def test_a_network_failure_is_retried_and_never_raises(self):
        # The thread runs off `_register_hook`; an exception escaping aborts
        # the registry load.
        self._run(side_effect=OSError("connection refused"))
        self.assertEqual(len(self.posts), enrollment._ATTEMPTS)

    def test_nothing_is_written_when_every_attempt_fails(self):
        self._run(response=_Resp(500, {}))
        self.assertFalse(self._param("orc.org_token"))
        self.assertFalse(self._param(enrollment._PARAM_ENROLL_DONE))
        # The secret survives, so the next restart reuses it rather than
        # publishing a hash nobody is going to be asked about.
        self.assertTrue(self._param(enrollment._PARAM_ENROLL_SECRET))

    # -- the multi-worker property ---------------------------------------

    def test_a_second_worker_reuses_the_committed_secret(self):
        # Regenerating per worker would clobber: A publishes S_A, B overwrites
        # with S_B, and A's submission fails against a host now serving
        # sha256(S_B). `claim_secret` must return what is COMMITTED, writing
        # only when there is nothing.
        with mock.patch.object(enrollment, "Registry",
                               lambda _db: _FakeRegistry(self.env.cr)):
            first = enrollment.claim_secret(DB)
            second = enrollment.claim_secret(DB)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(second, first, "a second caller must not regenerate")

    def test_claim_returns_the_stored_secret_it_did_not_write(self):
        # The case a same-transaction re-read could never see: the value was
        # committed by somebody else before this call began.
        planted = "9f" * 32
        self.ICP.set_param(enrollment._PARAM_ENROLL_SECRET, planted)
        with mock.patch.object(enrollment, "Registry",
                               lambda _db: _FakeRegistry(self.env.cr)):
            self.assertEqual(enrollment.claim_secret(DB), planted)

    def test_a_lost_write_race_re_reads_instead_of_failing(self):
        # The losing worker takes a unique violation or a serialization error.
        # Both mean the winner's value is committed, so the answer is to read
        # it in a FRESH transaction — not to give up and not to overwrite.
        planted = "7c" * 32
        calls = {"n": 0}

        class _Reg:
            def __init__(self, cr):
                self.cr = cr

            def cursor(_self):
                calls["n"] += 1
                if calls["n"] == 1:
                    # First transaction: pretend our write lost the race.
                    self.ICP.set_param(enrollment._PARAM_ENROLL_SECRET, planted)

                    class _Boom:
                        def __enter__(_s):
                            raise RuntimeError("duplicate key value violates key_uniq")

                        def __exit__(_s, *_a):
                            return False
                    return _Boom()
                return _FakeCursorCM(self.env.cr)

        with mock.patch.object(enrollment, "Registry", lambda _db: _Reg(self.env.cr)):
            self.assertEqual(enrollment.claim_secret(DB), planted)
        self.assertEqual(calls["n"], 2, "it must open a SECOND transaction to re-read")


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestSpentChallengeRecovery(TransactionCase):
    """A spent challenge with no stored credential must not be replayed forever.

    This is the failure the whole feature exists to end, reintroduced by a
    plausible shortcut: treating every 409 as "a sibling worker won". If a
    previous boot minted and then died before writing the token back, the
    credential is gone and that challenge can never be spent again — so
    replaying it leaves the build silently unreconnected.
    """

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        for key in ("orc.endpoint_url", "orc.org_token", "orc.infrastructure_id"):
            self.ICP.set_param(key, False)
        self.ICP.set_param(enrollment._PARAM_ENROLL_SECRET, "5a" * 32)
        self.ICP.set_param(enrollment._PARAM_ENROLL_DONE, False)

    def _run(self, response):
        with mock.patch.object(enrollment, "Registry",
                               lambda _db: _FakeRegistry(self.env.cr)), \
             mock.patch.object(enrollment.requests, "post",
                               side_effect=lambda url, **kw: response), \
             mock.patch.object(enrollment, "_repo_for", return_value="o/r"), \
             mock.patch.object(enrollment, "_provisioning_installed",
                               return_value=True), \
             mock.patch.object(enrollment.time, "sleep", lambda _s: None), \
             mock.patch.dict(enrollment.config.options,
                             {"test_enable": False, "test_file": False,
                              "stop_after_init": False}):
            enrollment._run_enrollment(DB, BASE)

    def test_a_spent_challenge_clears_the_secret_so_the_next_boot_starts_over(self):
        self._run(_Resp(409, {"error": "challenge already spent"}))
        self.assertFalse(
            (self.ICP.get_param(enrollment._PARAM_ENROLL_SECRET) or "").strip(),
            "a spent challenge must not be left to be replayed",
        )

    def test_a_mint_that_cannot_be_stored_clears_the_secret_too(self):
        # 200 with no infrastructure_id: the challenge is spent and the
        # credential is unusable. Replaying it would 409 forever.
        self._run(_Resp(200, {"ok": True, "token": "orc_" + "d" * 64}))
        self.assertFalse(
            (self.ICP.get_param(enrollment._PARAM_ENROLL_SECRET) or "").strip())
        self.assertFalse(
            (self.ICP.get_param(enrollment._PARAM_ENROLL_DONE) or "").strip(),
            "the build did not finish enrolling, so nothing may suppress a retry",
        )

    def test_a_mint_missing_infrastructure_id_never_reaches_apply(self):
        # The downstream `_apply` would raise on the missing key and the
        # recovery path would clear the secret anyway — so asserting only "the
        # secret was cleared" cannot tell the guard from the crash. Assert that
        # `_apply` is never entered.
        with mock.patch.object(enrollment, "_apply") as apply_:
            self._run(_Resp(200, {"ok": True, "token": "orc_" + "d" * 64}))
        apply_.assert_not_called()

    def test_a_200_without_a_token_is_not_treated_as_a_mint(self):
        self._run(_Resp(200, {"ok": False, "error": "no armed binding"}))
        self.assertFalse(
            (self.ICP.get_param("orc.org_token") or "").strip())


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestSkipReason(TransactionCase):

    def setUp(self):
        super().setUp()
        ICP = self.env["ir.config_parameter"].sudo()
        for key in ("orc.endpoint_url", "orc.org_token", "orc.infrastructure_id"):
            ICP.set_param(key, False)

    def test_skips_under_stop_after_init(self):
        with mock.patch.dict(enrollment.config.options,
                             {"stop_after_init": True, "test_enable": False,
                              "test_file": False}):
            self.assertEqual(enrollment._skip_reason(),
                             enrollment._SKIP_STOP_AFTER_INIT)

    def test_skips_in_test_mode(self):
        with mock.patch.dict(enrollment.config.options, {"test_enable": True}):
            self.assertEqual(enrollment._skip_reason(),
                             enrollment._SKIP_TEST_MODE)

    def test_the_runner_itself_refuses_in_test_mode(self):
        # Belt-and-braces parity with the reporter. An unguarded direct call
        # submits a LIVE proof and can have a real credential minted into
        # whatever database happens to be open.
        #
        # Everything else that could stop the POST is patched away on purpose:
        # the first version left `Registry` real, so the run died on a missing
        # database and the test passed with the guard deleted.
        with mock.patch.object(enrollment.requests, "post") as post, \
             mock.patch.object(enrollment, "Registry",
                               lambda _db: _FakeRegistry(self.env.cr)), \
             mock.patch.object(enrollment, "_repo_for", return_value="o/r"), \
             mock.patch.object(enrollment, "_provisioning_installed",
                               return_value=True), \
             mock.patch.dict(enrollment.config.options,
                             {"test_enable": True, "test_file": False,
                              "stop_after_init": False}):
            enrollment._run_enrollment(DB, BASE)
        post.assert_not_called()


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestSanitizeIfRebuilt(TransactionCase):
    """v15's stand-in for neutralize.sql.

    Odoo 15 has no neutralization machinery, so a rebuilt staging branch comes
    up holding whatever credentials its dump carried — normally production's.
    ``sanitize_if_rebuilt`` clears them by comparing the build the credentials
    were stamped for against the build actually running.

    The tests that matter most here are the negative ones. This function
    deletes live credentials, and the thing standing between it and a
    production database is the stage check.
    """

    OLD_BUILD = "pg-group-stage-11111111"
    SLUG, BUILD_ID = "pg-group-stage", "25407779"
    CURRENT = "pg-group-stage-25407779"

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        self._configure()

    def _configure(self, stamp=OLD_BUILD):
        """A build carrying a full set of credentials, stamped for `stamp`."""
        self.ICP.set_param("orc.endpoint_url", "https://help.opsway.com")
        self.ICP.set_param("orc.org_token", "orc_" + "p" * 64)
        self.ICP.set_param("orc.infrastructure_id", "prod-infra-uuid")
        self.ICP.set_param("orc.rotation_days", "30")
        self.ICP.set_param(enrollment._PARAM_BOUND_BUILD, stamp or False)

    def _sanitize(self, stage="staging"):
        env = {} if stage is None else {"ODOO_STAGE": stage}
        with mock.patch.object(enrollment, "Registry",
                               lambda _db: _FakeRegistry(self.env.cr)), \
             mock.patch.dict(os.environ, env, clear=True):
            return enrollment.sanitize_if_rebuilt(DB, self.SLUG, self.BUILD_ID)

    def _param(self, key):
        return (self.ICP.get_param(key) or "").strip()

    def _still_configured(self):
        return all(self._param(k) for k in (
            "orc.endpoint_url", "orc.org_token", "orc.infrastructure_id"))

    # -- it clears what it should -----------------------------------------

    def test_a_rebuilt_staging_build_drops_credentials_it_did_not_earn(self):
        self.assertTrue(self._sanitize("staging"))
        self.assertFalse(self._still_configured())
        # The stamp goes too, so a failed re-enrollment doesn't leave a
        # binding claim behind for the next boot to trust.
        self.assertFalse(self._param(enrollment._PARAM_BOUND_BUILD))

    def test_it_clears_this_addon_s_own_keys_as_well(self):
        # Especially the preimage: a live proof must not survive into a copy,
        # and `enroll_base` decides where the next proof is submitted.
        self.ICP.set_param(enrollment._PARAM_ENROLL_SECRET, "ab" * 32)
        self.ICP.set_param(enrollment._PARAM_ENROLL_DONE, "pg-group-stage:1")
        self.ICP.set_param(enrollment._PARAM_ENROLL_BASE, "https://evil.example")
        self.assertTrue(self._sanitize("staging"))
        for key in (enrollment._PARAM_ENROLL_SECRET,
                    enrollment._PARAM_ENROLL_DONE,
                    enrollment._PARAM_ENROLL_BASE):
            self.assertFalse(self._param(key), key)

    def test_dev_is_sanitizable_too(self):
        self.assertTrue(self._sanitize("dev"))
        self.assertFalse(self._still_configured())

    # -- and, far more importantly, what it must never touch ---------------

    def test_production_is_never_sanitized(self):
        # Odoo.sh production database names carry a build id that changes on
        # every deploy, so identity alone would have production delete its own
        # live credentials. Only the stage check stops that.
        self.assertFalse(self._sanitize("production"))
        self.assertTrue(self._still_configured())

    def test_production_records_ownership_instead(self):
        # The other half of the same guard, and the one a previous version got
        # wrong. Production never enrolls, so `_apply` never stamps it; if
        # stamping were skipped outside staging/dev as well, a production dump
        # would land on staging carrying NO stamp and keep production's
        # credentials for a whole rebuild cycle. Stamping is safe everywhere
        # because only the delete is stage-gated.
        self.assertFalse(self._sanitize("production"))
        self.assertEqual(self._param(enrollment._PARAM_BOUND_BUILD),
                         self.CURRENT)

    def test_an_unconfigured_production_build_claims_nothing(self):
        # No credentials means no ownership to record — a stamp here would
        # make a later rebuild delete something this build never held.
        for key in ("orc.endpoint_url", "orc.org_token",
                    "orc.infrastructure_id", enrollment._PARAM_BOUND_BUILD):
            self.ICP.set_param(key, False)
        self.assertFalse(self._sanitize("production"))
        self.assertFalse(self._param(enrollment._PARAM_BOUND_BUILD))

    def test_an_absent_stage_is_not_read_as_dev(self):
        # `get_stage()` answers "dev" when ODOO_STAGE is unset. Using it here
        # would make a missing environment variable destructive, so this path
        # reads the variable itself and requires a known value.
        self.assertFalse(self._sanitize(None))
        self.assertTrue(self._still_configured())

    def test_an_unrecognised_stage_does_nothing(self):
        self.assertFalse(self._sanitize("qa"))
        self.assertTrue(self._still_configured())

    def test_the_same_build_keeps_its_own_credentials(self):
        self._configure(stamp=self.CURRENT)
        self.assertFalse(self._sanitize("staging"))
        self.assertTrue(self._still_configured())

    def test_an_unstamped_build_is_adopted_rather_than_wiped(self):
        # A hand-configured instance never went through `_apply`, so it has no
        # stamp. Deleting on "no stamp" would wipe credentials we never issued;
        # instead it adopts the current build, and the NEXT rebuild sanitizes.
        self._configure(stamp=None)
        self.assertFalse(self._sanitize("staging"))
        self.assertTrue(self._still_configured())
        self.assertEqual(self._param(enrollment._PARAM_BOUND_BUILD),
                         self.CURRENT)

    def test_an_unconfigured_build_is_not_stamped(self):
        # Nothing to bind, so claiming a binding would be a lie the next
        # rebuild acts on.
        for key in ("orc.endpoint_url", "orc.org_token",
                    "orc.infrastructure_id", enrollment._PARAM_BOUND_BUILD):
            self.ICP.set_param(key, False)
        self.assertFalse(self._sanitize("staging"))
        self.assertFalse(self._param(enrollment._PARAM_BOUND_BUILD))

    def test_the_stale_list_matches_what_neutralize_sql_deletes(self):
        # The same policy expressed three times: this constant (v15), this
        # addon's neutralize.sql and provisioning's (both 16+). A key present
        # in one and missing from another is a silent hole, so the expected
        # set is written out HERE rather than derived from the code under
        # test — a list that reads itself can never catch a removal.
        addon = pathlib.Path(enrollment.__file__).parent.parent
        own_sql = (addon / "data" / "neutralize.sql").read_text()
        prov_sql = (addon.parent / "orc_client_provisioning"
                    / "data" / "neutralize.sql").read_text()

        provisioning_keys = [
            "orc.endpoint_url",
            "orc.org_token",
            "orc.infrastructure_id",
            "orc.rotation_days",
        ]
        own_keys = [
            "orc.enroll_secret",
            "orc_client_build_reporter.enroll_done_key",
            "orc_client_build_reporter.enroll_base",
        ]

        self.assertEqual(
            sorted(enrollment._STALE_ON_REBUILD),
            sorted(provisioning_keys + own_keys),
            "the v15 sanitizer must clear exactly the keys the two "
            "neutralize.sql files do — no more, and no fewer",
        )
        for key in own_keys:
            self.assertIn(f"'{key}'", own_sql,
                          f"{key} missing from this addon's neutralize.sql")
        for key in provisioning_keys:
            self.assertIn(f"'{key}'", prov_sql,
                          f"{key} missing from provisioning's neutralize.sql")


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestRebuildEndToEnd(TransactionCase):
    """The rebuild path all the way through: sanitize, then re-enroll."""

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.posts = []

    def _run(self, base=BASE, stage="staging"):
        def _post(url, **kw):
            self.posts.append({"url": url, **kw})
            return _Resp(200, MINTED)

        with mock.patch.object(enrollment, "Registry",
                               lambda _db: _FakeRegistry(self.env.cr)), \
             mock.patch.object(enrollment.requests, "post", side_effect=_post), \
             mock.patch.object(enrollment, "_repo_for",
                               return_value="opsway/pg_group"), \
             mock.patch.object(enrollment, "_provisioning_installed",
                               return_value=True), \
             mock.patch.object(enrollment.time, "sleep", lambda _s: None), \
             mock.patch.dict(os.environ, {"ODOO_STAGE": stage}, clear=True), \
             mock.patch.dict(enrollment.config.options,
                             {"test_enable": False, "test_file": False,
                              "stop_after_init": False}):
            enrollment._run_enrollment(DB, base)

    def _param(self, key):
        return (self.ICP.get_param(key) or "").strip()

    def test_a_successful_enrollment_stamps_the_build_it_bound(self):
        for key in ("orc.endpoint_url", "orc.org_token", "orc.infrastructure_id"):
            self.ICP.set_param(key, False)
        self._run()
        self.assertEqual(self._param(enrollment._PARAM_BOUND_BUILD),
                         "pg-group-stage-25407779")

    def test_a_rebuilt_build_sanitizes_then_gets_its_own_credential(self):
        # The whole point: before this, a v15 staging rebuild sat on
        # production's token forever because `_needs_enrollment` was false.
        self.ICP.set_param("orc.endpoint_url", "https://help.opsway.com")
        self.ICP.set_param("orc.org_token", "orc_" + "p" * 64)
        self.ICP.set_param("orc.infrastructure_id", "prod-infra-uuid")
        self.ICP.set_param(enrollment._PARAM_BOUND_BUILD,
                           "pg-group-stage-11111111")
        self._run()
        self.assertTrue(self.posts, "a rebuilt build must ask for a credential")
        self.assertEqual(self._param("orc.org_token"), MINTED["token"])
        self.assertEqual(self._param(enrollment._PARAM_BOUND_BUILD),
                         "pg-group-stage-25407779")

    def test_a_production_dump_on_staging_is_sanitized_on_the_first_boot(self):
        # The case the feature actually has to handle, and the one an earlier
        # version of the sanitizer missed. Production never enrolls, so its
        # credentials are stamped by the ownership pass, not by `_apply`. That
        # stamp is what the staging build restoring the dump compares against.
        # Without it, staging found no stamp, adopted, and kept production's
        # token until the next rebuild.
        self.ICP.set_param("orc.endpoint_url", "https://help.opsway.com")
        self.ICP.set_param("orc.org_token", "orc_" + "p" * 64)
        self.ICP.set_param("orc.infrastructure_id", "prod-infra-uuid")
        self.ICP.set_param(enrollment._PARAM_BOUND_BUILD, False)

        # 1. Production boots and records that the credentials are its own.
        with mock.patch.object(enrollment, "Registry",
                               lambda _db: _FakeRegistry(self.env.cr)), \
             mock.patch.dict(os.environ, {"ODOO_STAGE": "production"},
                             clear=True):
            enrollment.sanitize_if_rebuilt(
                "claimex-prod", "claimex-prod", "4321978")
        self.assertEqual(self._param(enrollment._PARAM_BOUND_BUILD),
                         "claimex-prod-4321978")

        # 2. That dump is restored onto a staging build, which boots and must
        #    refuse the inherited credentials on the FIRST run.
        self._run()
        self.assertTrue(
            self.posts,
            "staging must ask for its own credential on the first boot")
        self.assertEqual(self._param("orc.org_token"), MINTED["token"])
        self.assertEqual(self._param(enrollment._PARAM_BOUND_BUILD),
                         "pg-group-stage-25407779")

    def test_a_restored_enroll_base_override_is_not_honoured_for_one_boot(self):
        # `enroll_base` is resolved in `_register_hook`, BEFORE the parameters
        # are known to be stale — so a hostile override riding in on a dump
        # would otherwise be used for exactly the one boot that matters. After
        # sanitizing, the base is re-resolved and the in-source constant wins.
        hostile = "https://evil.example/webhook/odoo-sh/enroll"
        self.ICP.set_param(enrollment._PARAM_ENROLL_BASE, hostile)
        self.ICP.set_param("orc.endpoint_url", "https://help.opsway.com")
        self.ICP.set_param("orc.org_token", "orc_" + "p" * 64)
        self.ICP.set_param("orc.infrastructure_id", "prod-infra-uuid")
        self.ICP.set_param(enrollment._PARAM_BOUND_BUILD,
                           "pg-group-stage-11111111")

        self._run(base=hostile)

        self.assertTrue(self.posts)
        for post in self.posts:
            self.assertNotIn("evil.example", post["url"])
            self.assertTrue(post["url"].startswith(enrollment.ENROLL_BASE))
        # And the derived endpoint follows the base that was actually used.
        self.assertEqual(self._param("orc.endpoint_url"),
                         "https://help.opsway.com")
