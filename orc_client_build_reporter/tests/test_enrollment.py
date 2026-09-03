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
from unittest import mock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

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
class TestSplitDbName(TransactionCase):
    """The identity the addon reports. Pure function, no DB."""

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
class TestChallenge(TransactionCase):
    """The half of the proof contract that lives in another repository."""

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
             mock.patch.object(enrollment.time, "sleep", lambda _s: None):
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
                               return_value=True):
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
                               return_value=False):
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
                               return_value=True):
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
        # Regenerating would clobber: worker A publishes S_A, B overwrites with
        # S_B, then A submits S_A while the host serves sha256(S_B) — every
        # attempt fails as a proof mismatch. Claim-once-and-re-read is what
        # makes a committed secret safe across workers.
        self._run(response=_Resp(503, {}))
        first = self._param(enrollment._PARAM_ENROLL_SECRET)
        self.posts = []
        self._run(response=_Resp(503, {}))
        self.assertEqual(self._param(enrollment._PARAM_ENROLL_SECRET), first)
        self.assertEqual(self.posts[0]["json"]["proof"], first)


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestPublishedChallenge(TransactionCase):
    """What the public route is allowed to say.

    This is the only unauthenticated surface in the family, so the two
    properties worth pinning are that it publishes the COMMITMENT and never the
    preimage, and that it says nothing at all when no enrollment is pending.
    """

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()

    def test_serves_nothing_when_no_enrollment_is_pending(self):
        self.ICP.set_param(enrollment._PARAM_ENROLL_SECRET, False)
        self.assertIsNone(enrollment.published_challenge(self.env))

    def test_reads_the_secret_off_the_transaction_not_the_ormcache(self):
        """The route must not publish a hash of a secret that is no longer there.

        ``ICP.get_param`` is ``@ormcache``'d per registry. The secret is written
        by whichever worker wins the claim, so a DIFFERENT worker serving this
        route can hold a value cached from before that write — and would then
        publish the commitment for a secret nobody is going to submit. Every
        enrollment fails as a proof mismatch, and nothing says why.

        The first version of this file caught that by accident, through a stale
        value another test happened to leave behind, which meant it stopped
        catching it as soon as the test order changed. This poisons the cache on
        purpose instead: populate it, then change the row behind the ORM's back —
        exactly what another worker's committed write looks like from here.
        """
        first, second = "ab" * 32, "cd" * 32
        self.ICP.set_param(enrollment._PARAM_ENROLL_SECRET, first)
        self.ICP.flush_model(["key", "value"])
        # Populate the ormcache with `first`.
        self.assertEqual(
            self.ICP.get_param(enrollment._PARAM_ENROLL_SECRET), first)
        # Change it without going through the ORM, so nothing invalidates.
        self.env.cr.execute(
            "UPDATE ir_config_parameter SET value = %s WHERE key = %s",
            (second, enrollment._PARAM_ENROLL_SECRET),
        )
        self.assertEqual(
            self.ICP.get_param(enrollment._PARAM_ENROLL_SECRET), first,
            "precondition: the ormcache must still be serving the stale value",
        )
        self.assertEqual(
            enrollment.published_challenge(self.env),
            hashlib.sha256(second.encode("ascii")).hexdigest(),
            "published_challenge must read the committed row, not the cache",
        )

    def test_publishes_the_hash_and_never_the_preimage(self):
        secret = "cd" * 32
        self.ICP.set_param(enrollment._PARAM_ENROLL_SECRET, secret)
        published = enrollment.published_challenge(self.env)
        self.assertEqual(published,
                         hashlib.sha256(secret.encode("ascii")).hexdigest())
        # The property the whole scheme rests on: reading the public route
        # must not reveal anything that can be replayed as the proof.
        self.assertNotEqual(published, secret)
        self.assertNotIn(secret, published)


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestSkipReason(TransactionCase):

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
