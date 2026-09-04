"""Self-enrollment: a recycled Odoo.sh build reconnects itself to AI Workplace.

Odoo.sh rebuilds a staging branch roughly monthly, and again on every "new
build" push. Each rebuild restores a dump and NEUTRALIZES it, which deletes the
four ``orc.*`` parameters ``orc_client_provisioning`` needs and disables every
cron. Until now an operator reconnected by hand every time, and the failure was
silent: nothing raises, nothing alerts, and it is usually somebody noticing the
agent cannot see their staging data that surfaces it weeks later.

This module lets a build that can PROVE IT IS ITSELF reconnect unattended.

Why this lives in the build reporter, and not in provisioning
-------------------------------------------------------------

The state enrollment produces is provisioning's — the four ``orc.*``
parameters and the two crons. The obvious home is therefore provisioning, and
it is the wrong one, because every INPUT is here:

* ``get_project_root`` + ``get_repo_from_git`` — the reported ``repo``. This is
  not a trivial helper. It climbs out of any submodule layer to the customer's
  outermost working tree, because reading the addon directory's own origin
  reports ``opsway/odoo-orc-client`` at the pinned sub-SHA and Workplace
  rejects the report. ``TestGetProjectRoot`` pins both layouts. Copying it into
  provisioning would be this codebase's "one rule, two copies" failure class
  with a genuinely subtle rule.
* the ``_register_hook`` daemon thread — the only trigger that survives a
  neutralize, since crons are disabled, migrations need a version bump, and XML
  data loads only on install/upgrade.
* ``_skip_reason`` — the ``--stop-after-init`` guard, which is load-bearing for
  exactly the same reason here as it is for the report.
* the database-name split, and the advisory-lock idiom.

What it writes back is a handful of ``ir.config_parameter`` values and two
``env.ref`` lookups, which need no code dependency on provisioning at all and
degrade to a no-op when it is absent. So the coupling is one-directional and
cheap in this direction, and expensive in the other.

**This does change the character of this addon**, and the change is worth
stating: it now RECEIVES a credential. ``AGENTS.md`` records "no headers
carrying secrets … there is nothing to log-redact", which stays true of the
report and is no longer true of the module. The minted token is never logged
here — only its last six characters — and the preimage is never logged at all.

The proof
---------

Hash-publish / preimage-submit, not plain HTTP-01::

    1. generate S            64 lowercase hex characters (32 random bytes)
    2. publish sha256(S)     GET /orc/enroll/challenge  (public, read-only)
    3. submit S              POST {base}

AI Workplace derives the host ITSELF from the reported branch slug and build
id, fetches the published hash from that host, and checks it against the
submitted preimage. Plain HTTP-01 fails here for a specific reason: a publicly
readable nonce can be read by anyone who can reach this build, and the response
hands back a credential — so whoever read the nonce first would get the token.
Publishing the hash and submitting the preimage inverts that: reading the
challenge tells an attacker nothing they can replay.

**The hash covers the ASCII TEXT of the hex string, not the 32 decoded bytes.**
The verifying half lives in another repository with no shared code, so the
contract is pinned in prose on both sides; hashing the text is the one choice
with no decoding step for either side to get wrong.

Why the secret is COMMITTED before the POST
-------------------------------------------

This is the opposite of what the reporter beside it does, deliberately, and the
difference is not stylistic.

The reporter holds one transaction across its POST — advisory lock, claim,
post, stamp — so its debounce is mutual. It can, because nothing reads its
state from outside that transaction.

Enrollment cannot. AI Workplace calls BACK into this Odoo, onto an arbitrary
worker over a different connection, to read the published challenge. A secret
written but not yet committed is invisible to that worker, so holding the
transaction across the POST would fail whenever there is more than one worker:
green on single-worker dev, dead on Odoo.sh. That is the same cross-cursor
REPEATABLE READ class as the 18.0.1.13.1 orphaned-key bug. So the secret is
written and committed FIRST, and only then submitted.

Which raises the question the reporter's lock exists to answer: if the
transaction ends before the POST, what stops N workers clobbering each other's
secret? Worker A publishes S_A, B overwrites with S_B, then A submits S_A while
the host now serves sha256(S_B) — every attempt fails as a proof mismatch.

The answer is that **the secret is claimed once and re-read, never
regenerated**. Under an advisory lock, in a SHORT transaction that commits
immediately: read the secret off the transaction, generate one only if absent,
commit. Every worker then publishes and submits the SAME S. They race, and that
race is already decided on the server: the challenge is single-use, anchored by
a unique index on its digest, so exactly one worker mints and the rest are told
the challenge is already spent. That is a normal outcome here, not an error —
the environment got its credential.
"""

import hashlib
import logging
import os
import secrets
import threading
import time
from urllib.parse import urlparse

import requests

from odoo import SUPERUSER_ID, api, models
from odoo.modules.registry import Registry
from odoo.tools import config

from .build_reporter import (
    get_project_root,
    get_repo_from_git,
    parse_dev_url,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SELF-HOSTERS: point this at your own AI Workplace deployment.
#
# The ICP key below overrides it for one-off testing, but the CONSTANT is what
# matters: it survives a neutralize. The endpoint a wiped build asks for a
# credential cannot itself live in a parameter the wipe deleted.
# ---------------------------------------------------------------------------
ENROLL_BASE = "https://help.opsway.com/webhook/odoo-sh/enroll"

_PARAM_ENROLL_BASE = "orc_client_build_reporter.enroll_base"
# The preimage. Deleted by neutralize.sql — a live secret must never ride a
# dump into another database — and deleted again on success.
_PARAM_ENROLL_SECRET = "orc.enroll_secret"
# `<branch_slug>:<build_id>`, stamped only after a successful enrollment so a
# reconnected build does not enroll again on every restart.
_ENV_BUILD_URL = "ODOO_BUILD_URL"
_PARAM_ENROLL_DONE = "orc_client_build_reporter.enroll_done_key"

_ATTEMPTS = 3
_BACKOFF_SECONDS = (2, 5)
# ABOVE the server's own budget for this request, not below it. Workplace
# spends up to 6s fetching our challenge plus 6s on the mint, under a 15s nginx
# cap — so a 10s client timeout, which is what this used to be, gives up while a
# mint is still in flight. That is the worst possible moment to stop listening:
# the challenge is spent and the credential is issued to nobody. Wait past the
# cap so we receive the server's answer, whatever it is.
_POST_TIMEOUT = 20

_SKIP_TEST_MODE = "test mode"
_SKIP_STOP_AFTER_INIT = (
    "--stop-after-init: Odoo.sh's build phase tears the connection pool down "
    "without waiting for daemon threads; the serving process that starts next "
    "enrolls instead"
)


def split_db_name(dbname):
    """``<branch_slug>-<build_id>`` -> ``(slug, build_id)``, else ``None``.

    This is the whole of what enrollment reports about its own identity: AI
    Workplace derives the hostname it will dial from these two fields and
    deliberately never takes it from the request body.

    Note what this is NOT: another copy of ``_DEV_HOST_RE``. That regex parses
    a URL authority, because the reporter reports a URL. Enrollment reports no
    host at all, so a database-name split is the entire requirement.

    >>> split_db_name("pg-group-stage-25407779")
    ('pg-group-stage', '25407779')
    >>> split_db_name("odoo") is None
    True
    >>> split_db_name("-123") is None
    True
    """
    parts = (dbname or "").rsplit("-", 1)
    if len(parts) == 2 and parts[0] and parts[1].isdigit():
        return parts[0], parts[1]
    return None


def challenge_for(secret):
    """The published challenge: sha256 over the ASCII TEXT of the hex secret.

    See the module docstring — the verifying half is in another repository, and
    hashing the text rather than the decoded bytes removes the one step the two
    implementations could disagree about.

    >>> challenge_for("ab" * 32)[:16]
    '271a413bd339c570'
    """
    return hashlib.sha256((secret or "").encode("ascii")).hexdigest()


def _resolve_enroll_base(env):
    """ICP value wins if set; the in-source constant is the fallback.

    Same shape as the reporter's ``_resolve_webhook_base``, and for the same
    reason: the constant is the half that survives a wiped database.
    """
    icp_value = (env["ir.config_parameter"].sudo().get_param(
        _PARAM_ENROLL_BASE) or "").strip()
    return icp_value or (ENROLL_BASE or "").strip() or None


def _origin_of(url):
    """``https://host[:port]`` for a URL, or ``None``.

    >>> _origin_of("https://help.opsway.com/webhook/odoo-sh/enroll")
    'https://help.opsway.com'
    >>> _origin_of("nonsense") is None
    True
    """
    try:
        parts = urlparse(url or "")
    except Exception:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def published_challenge(env):
    """The value the public route serves, or ``None`` when none is pending.

    The controller asks for this rather than reading the parameter itself: the
    secret's storage key is an implementation detail of enrollment, and the
    route's job is to publish a commitment, not to know where the preimage
    lives. Keeping the read here also keeps the ``sha256``-of-the-hex-TEXT rule
    in one place, which matters because the verifying half is in another
    repository.
    """
    # Read off the TRANSACTION, not through ``ICP.get_param``.
    #
    # ``get_param`` is ``@ormcache``'d per registry, and the first version of
    # this helper used it. A test caught the consequence, but the production
    # shape is worse than the test one: the secret is written by whichever
    # worker wins the claim, so a DIFFERENT worker serving this route can hold
    # a cached value from before that write and publish the hash of a secret
    # nobody is going to submit. Every enrollment then fails as a proof
    # mismatch, and nothing anywhere says why. The same staleness bites again
    # after a successful enrollment deletes the secret, where the route would
    # keep publishing a commitment for something that no longer exists.
    #
    # Everything else in this module already reads parameters this way; the
    # helper simply has to do the same.
    _flush_params(env)
    secret = _params(env.cr, [_PARAM_ENROLL_SECRET]).get(_PARAM_ENROLL_SECRET)
    return challenge_for(secret) if secret else None


def _skip_reason():
    """Why enrollment must not run in this process, or ``None`` to proceed.

    Identical policy to the reporter's, and load-bearing for the same reason:
    ``--stop-after-init`` is Odoo.sh's build phase, which tears the connection
    pool down without waiting for daemon threads. A thread still holding a
    cursor there logs an ERROR beneath this addon's try/except and reds the
    whole build.
    """
    if config.get("test_enable") or config.get("test_file"):
        return _SKIP_TEST_MODE
    if config.get("stop_after_init"):
        return _SKIP_STOP_AFTER_INIT
    return None


def _params(cr, keys):
    """Read parameters straight off the transaction.

    Deliberately not ``ICP.get_param``: that is ``@ormcache``'d on the key, so
    it can hand back a value this process cached before another worker wrote a
    different one — exactly the staleness the lock exists to eliminate.
    """
    cr.execute(
        "SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)",
        (list(keys),),
    )
    return {k: (v or "").strip() for k, v in cr.fetchall()}


def _flush_params(env):
    """Push pending ORM writes down so a raw SELECT can see them.

    Core's own ``_get_param`` does the same. Without it a ``set_param`` made
    earlier in this transaction is invisible to ``_params``.
    """
    env["ir.config_parameter"].sudo().flush_model(["key", "value"])


def _provisioning_installed(env):
    """Is there anything here that would READ the credential we are asking for?

    Enrolling without ``orc_client_provisioning`` would mint a real token and
    park it in parameters nothing reads — a live credential created for no
    reason, which is the kind of thing that is only ever discovered during an
    incident.
    """
    return bool(env["ir.module.module"].sudo().search_count([
        ("name", "=", "orc_client_provisioning"),
        ("state", "=", "installed"),
    ]))


def _needs_enrollment(cfg):
    """True when this Odoo cannot talk to AI Workplace.

    The same three parameters ``orc.client._config`` requires. Enrollment is a
    REPAIR, not a provisioning path: a configured build must never re-enroll,
    because that would mint a fresh token and supersede the working one on
    every restart.
    """
    return not all(
        cfg.get(k) for k in
        ("orc.endpoint_url", "orc.org_token", "orc.infrastructure_id")
    )


def claim_secret(dbname):
    """The committed preimage for this build, in its OWN short transaction.

    Writes one only if none is committed yet, and returns whatever is committed
    when it finishes. The transaction is short and separate on purpose: a NEW
    transaction takes a NEW snapshot, which is the only way to see a value
    another worker committed after ours began.

    ## Why there is no advisory lock here any more

    The first version took `pg_try_advisory_xact_lock` and re-read the value
    "under the lock", claiming a loser would then see the winner's secret.
    Measured against Postgres, that is false: Odoo cursors run at REPEATABLE
    READ, the snapshot is fixed by the transaction's first statement, and a
    re-read later in the same transaction returns `<invisible>` for a row
    committed after it. The lock excluded writers but the re-read could never
    observe what they wrote, so the safety argument the module documented did
    not hold.

    An `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` upsert does not rescue
    it either — under REPEATABLE READ that raises `could not serialize access
    due to concurrent update` rather than handing back the winner's row. Both
    were measured, not reasoned about.

    So the design stops pretending to be mutually exclusive and becomes
    CONVERGENT, which is all this actually needs:

      * a worker writes a secret only when it sees none committed;
      * a worker that loses the write race catches the failure and re-reads in
        a fresh transaction, where the winner's value IS visible;
      * every POST attempt re-reads before submitting, so a worker that read a
        value which has since been replaced submits the current one on its next
        attempt rather than a stale one.

    What is left unhandled is deliberately small: in the brief window where two
    workers both see nothing, one write wins and the other re-reads it. Nobody
    publishes a hash of a secret that is not the committed one for longer than
    a single attempt.
    """
    try:
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            existing = _params(cr, [_PARAM_ENROLL_SECRET]).get(_PARAM_ENROLL_SECRET)
            if existing:
                return existing
            secret = secrets.token_hex(32)
            env["ir.config_parameter"].sudo().set_param(_PARAM_ENROLL_SECRET, secret)
            return secret
    except Exception as e:
        # Lost the write race: a unique violation on `key`, or a serialization
        # failure. Either way the winner's value is committed, and a FRESH
        # transaction is what makes it readable.
        _logger.info("[orc_enrollment] lost the secret write race (%s); "
                     "re-reading the committed one", e)
        try:
            with Registry(dbname).cursor() as cr:
                return _params(cr, [_PARAM_ENROLL_SECRET]).get(_PARAM_ENROLL_SECRET)
        except Exception:
            return None


def _params(cr, keys):
    """Read parameters straight off the transaction.

    Deliberately not ``ICP.get_param``: that is ``@ormcache``'d on the key, so
    it can hand back a value this process cached before another worker wrote a
    different one — exactly the staleness the lock exists to eliminate.
    """
    cr.execute(
        "SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)",
        (list(keys),),
    )
    return {k: (v or "").strip() for k, v in cr.fetchall()}


def _flush_params(env):
    """Push pending ORM writes down so a raw SELECT can see them.

    Core's own ``_get_param`` does the same. Without it a ``set_param`` made
    earlier in this transaction is invisible to ``_params``.
    """
    env["ir.config_parameter"].sudo().flush_model(["key", "value"])


def _provisioning_installed(env):
    """Is there anything here that would READ the credential we are asking for?

    Enrolling without ``orc_client_provisioning`` would mint a real token and
    park it in parameters nothing reads — a live credential created for no
    reason, which is the kind of thing that is only ever discovered during an
    incident.
    """
    return bool(env["ir.module.module"].sudo().search_count([
        ("name", "=", "orc_client_provisioning"),
        ("state", "=", "installed"),
    ]))


def _needs_enrollment(cfg):
    """True when this Odoo cannot talk to AI Workplace.

    The same three parameters ``orc.client._config`` requires. Enrollment is a
    REPAIR, not a provisioning path: a configured build must never re-enroll,
    because that would mint a fresh token and supersede the working one on
    every restart.
    """
    return not all(
        cfg.get(k) for k in
        ("orc.endpoint_url", "orc.org_token", "orc.infrastructure_id")
    )


def _forget_secret(dbname, done_key):
    """Drop the challenge secret so the next boot starts a fresh enrollment.

    Called wherever a challenge is spent but no credential was stored. Without
    it the next boot republishes the same commitment, is told the challenge is
    already spent, and reads that as somebody else's success — a build that
    never reconnects and never says why.

    `enroll_done_key` is cleared too: this build did NOT finish enrolling, and
    leaving the stamp would suppress the retry it needs.
    """
    try:
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            ICP = env["ir.config_parameter"].sudo()
            ICP.set_param(_PARAM_ENROLL_SECRET, False)
            if ICP.get_param(_PARAM_ENROLL_DONE) == done_key:
                ICP.set_param(_PARAM_ENROLL_DONE, False)
    except Exception:
        _logger.exception("[orc_enrollment] could not clear the challenge secret")


def _apply(env, minted):
    """Write the four parameters and re-enable the two crons.

    Core's neutralize disables EVERY cron, so a build that reconnects but stays
    silent is not actually reconnected: roles never sync and keys never rotate.
    Crons are looked up by xmlid and a missing one is ignored, so a renamed
    record cannot fail a reconnection that already succeeded.
    """
    ICP = env["ir.config_parameter"].sudo()
    ICP.set_param("orc.endpoint_url", minted["endpoint_url"])
    ICP.set_param("orc.org_token", minted["token"])
    ICP.set_param("orc.infrastructure_id", minted["infrastructure_id"])
    # `orc.rotation_days` IS deleted by `orc_client_provisioning`'s own
    # neutralize.sql (it names all four `orc.*` keys), so on a neutralized
    # build this branch is the normal path, not the exception. Restoring the
    # documented default keeps key rotation working after a reconnect.
    if not (ICP.get_param("orc.rotation_days") or "").strip():
        ICP.set_param("orc.rotation_days", "30")

    for xmlid in (
        "orc_client_provisioning.ir_cron_orc_sync",
        "orc_client_provisioning.ir_cron_orc_maintenance",
    ):
        cron = env.ref(xmlid, raise_if_not_found=False)
        if cron and not cron.active:
            cron.sudo().write({"active": True})

    # The preimage has done its job. Leaving it parks a live secret in a
    # database that gets dumped and restored elsewhere.
    ICP.set_param(_PARAM_ENROLL_SECRET, False)


def _post_enrollment(url, body, attempt):
    """One submission. Returns ``(outcome, payload)``.

    ``outcome`` is ``"minted"``, ``"spent"`` (another worker won — a success
    for the environment, not an error), or ``"retry"``.
    """
    r = requests.post(
        url, json=body, timeout=_POST_TIMEOUT,
        headers={
            "User-Agent": "orc-client-enrollment/1.0",
            "Accept": "application/json",
        },
    )
    if r.status_code == 200:
        data = r.json()
        if data.get("ok") and data.get("token"):
            return "minted", data
        return "retry", None
    # 409 is the single-use anchor doing its job: some worker of this same
    # build already spent this challenge and is applying the credential.
    if r.status_code == 409:
        # NOT unconditionally "a sibling worker won". The mint boundary also
        # answers 409 for `ambiguous binding for this branch` (two armed
        # infrastructures share this slug) and for a stale build — operator
        # misconfigurations that a retry will never fix and that must not be
        # logged as somebody else's success. It passes its reason through
        # rather than flattening it, so read it.
        try:
            reason = str(r.json().get("error") or "")
        except Exception:
            reason = ""
        if "already spent" in reason:
            return "spent", None
        _logger.warning(
            "[orc_enrollment] refused (409): %s — this needs an operator, "
            "not a retry", reason or "<no reason given>")
        return "stop", None
    # Only the server's own `error` field, never the raw body. The verifying
    # side does not echo the request today, but a body that ever did would put
    # the PREIMAGE in the build log — and the module docstring promises it is
    # never logged. Parse, do not splat.
    try:
        detail = str(r.json().get("error"))[:200]
    except Exception:
        detail = "<unparseable body>"
    _logger.warning(
        "[orc_enrollment] attempt %s refused: HTTP %s %s",
        attempt, r.status_code, detail,
    )
    # 4xx other than 409 is a decision, not a hiccup — retrying an unarmed
    # binding or a mismatched proof just burns attempts against a settled
    # answer. 5xx and timeouts are worth another try.
    return ("retry" if r.status_code >= 500 else "stop"), None


def _run_enrollment(dbname, enroll_base):
    """Publish a challenge, prove it, and write back what AI Workplace mints.

    Wrapped so nothing escapes into startup. Every early return is a normal
    outcome: not an Odoo.sh build, already configured, already enrolled for
    this build, or provisioning not installed.
    """
    try:
        # Belt-and-braces: `_register_hook` already refuses to spawn this
        # thread under these conditions. Repeated here so a direct call
        # (tests, `odoo shell`) obeys them too — and the stakes are higher than
        # the reporter's, because an unguarded direct call does not post a
        # report, it submits a live proof and can have a real credential minted
        # into whatever database is open.
        reason = _skip_reason()
        if reason:
            if reason is not _SKIP_TEST_MODE:
                _logger.info("[orc_enrollment] skip: %s", reason)
            return

        # `ODOO_BUILD_URL` first, database name second — the same precedence
        # the reporter uses, and for a stronger reason here: Workplace builds
        # the host it dials as `<branch_slug>-<build_id>.dev.odoo.com`, so this
        # field IS host-determining. Where the two disagree, the build URL is
        # the one that matches the host actually serving our challenge; taking
        # the database name would send Workplace to a host that 502s.
        env_build_url = os.environ.get(_ENV_BUILD_URL) or ""
        split = parse_dev_url(env_build_url) if env_build_url else None
        if not split:
            split = split_db_name(dbname)
        if not split:
            # Not an Odoo.sh build database. Self-hosted installs configure the
            # addon by hand and must never have a token minted at them.
            return
        branch_slug, build_id = split
        done_key = f"{branch_slug}:{build_id}"

        # ---- 1. Decide, and claim the secret. Short transaction; COMMITS. --
        #
        # The commit is the point. AI Workplace reads the published challenge
        # from an arbitrary worker over a different connection, and cannot see
        # an uncommitted value. See the module docstring.
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            if not _provisioning_installed(env):
                return
            cfg = _params(cr, [
                "orc.endpoint_url", "orc.org_token", "orc.infrastructure_id",
                _PARAM_ENROLL_SECRET, _PARAM_ENROLL_DONE,
            ])
            if not _needs_enrollment(cfg):
                return
            if cfg.get(_PARAM_ENROLL_DONE) == done_key:
                # This build already enrolled and its config has since gone
                # missing again. Re-enrolling would loop against whatever is
                # deleting it; an operator needs to look instead.
                _logger.warning(
                    "[orc_enrollment] %s already enrolled but config is "
                    "missing again — not re-enrolling; investigate what is "
                    "clearing the orc.* parameters",
                    done_key,
                )
                return
            pass

        # ---- 2. Validate what we will send, BEFORE claiming a secret -------
        #
        # Order matters. Claiming writes and commits a secret, and the public
        # route starts publishing its hash immediately. Discovering only
        # afterwards that we cannot build a request leaves a live commitment
        # that is never proved and that nothing clears.
        endpoint_url = _origin_of(enroll_base)
        if not endpoint_url:
            _logger.warning(
                "[orc_enrollment] enrollment base %r has no usable origin",
                enroll_base)
            return
        repo = _repo_for()
        if not repo:
            _logger.warning("[orc_enrollment] cannot derive repo from the "
                            "project's origin URL; not enrolling")
            return

        # ---- 3. Prove it --------------------------------------------------
        minted = None
        for attempt in range(1, _ATTEMPTS + 1):
            # Re-read every attempt, in its own transaction. If another worker
            # replaced the secret after we first read it, the public route is
            # now serving THAT one's hash — so submitting what we read the
            # first time would fail forever. Reading again is what makes the
            # convergent claim converge.
            secret = claim_secret(dbname)
            if not secret:
                _logger.warning("[orc_enrollment] no challenge secret available")
                return
            body = {
                "repo": repo,
                "branch_slug": branch_slug,
                "build_id": build_id,
                "proof": secret,
            }
            try:
                outcome, data = _post_enrollment(enroll_base, body, attempt)
            except Exception as e:
                outcome, data = "retry", None
                _logger.info("[orc_enrollment] attempt %s failed: %s", attempt, e)
            if outcome == "minted":
                minted = data
                break
            if outcome == "spent":
                # Some worker spent this challenge. Usually a sibling of this
                # same build, which is applying the credential right now — but
                # NOT always, and assuming so was a real defect: if a previous
                # boot minted and then died before writing the token back, the
                # credential is gone and this challenge can never be spent
                # again. Replaying it forever would leave the build silently
                # unreconnected, which is the exact failure this feature exists
                # to end. Clearing the secret makes the next boot start a fresh
                # challenge instead of replaying a dead one.
                _logger.info("[orc_enrollment] challenge already spent")
                _forget_secret(dbname, done_key)
                return
            if outcome == "stop":
                return
            if attempt < _ATTEMPTS:
                # Bounded, because "retry on the next restart" is too weak
                # here: Workplace calls back seconds after registry init, and
                # if the HTTP frontend is not serving yet the challenge fetch
                # fails. Without a retry the build stays dead until somebody
                # restarts it by hand — reintroducing the manual step this
                # feature removes.
                time.sleep(_BACKOFF_SECONDS[min(attempt - 1,
                                                len(_BACKOFF_SECONDS) - 1)])

        if not minted:
            _logger.warning("[orc_enrollment] gave up after %s attempts; the "
                            "next restart will try again", _ATTEMPTS)
            return

        # The mint is spent the moment Workplace answers, so anything missing
        # here is a credential we can never ask for again. Check before we
        # start writing rather than half-applying and losing it.
        if not minted.get("infrastructure_id"):
            _logger.error(
                "[orc_enrollment] mint response has no infrastructure_id; the "
                "challenge is spent and this credential is lost. The next boot "
                "will start a fresh enrollment.")
            _forget_secret(dbname, done_key)
            return

        # ---- 4. Apply. Second short transaction. -------------------------
        try:
            with Registry(dbname).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                _apply(env, dict(minted, endpoint_url=endpoint_url))
                env["ir.config_parameter"].sudo().set_param(
                    _PARAM_ENROLL_DONE, done_key)
        except Exception:
            # The credential existed only in this thread. Left alone, the next
            # boot would replay a spent challenge, be told so, and treat that
            # as somebody else's success — for ever. Say so loudly and clear
            # the secret so the next boot asks for a NEW credential.
            _logger.exception(
                "[orc_enrollment] minted a credential but failed to store it; "
                "starting over on the next boot")
            _forget_secret(dbname, done_key)
            return
        _logger.info(
            "[orc_enrollment] enrolled %s: infrastructure=%s token=...%s",
            done_key, minted.get("infrastructure_id"),
            (minted.get("token") or "")[-6:],
        )
    except Exception as e:
        # Never re-raise. `_register_hook` runs inside the module-loading
        # transaction, and an exception escaping it aborts the registry load.
        _logger.warning("[orc_enrollment] failed: %s", e)


def _repo_for():
    """The customer project's ``owner/repo``, or ``None``.

    Reuses the reporter's derivation rather than repeating it: it climbs out of
    any submodule layer to the outermost working tree, because reading the
    addon directory's own origin reports ``opsway/odoo-orc-client`` at the
    pinned sub-SHA. ``TestGetProjectRoot`` pins both layouts.

    Diverges from the reporter in one way, deliberately: the reporter falls
    back to the addon directory when the climb fails, and this does not. A
    report carrying the wrong repo is a row Workplace rejects; an ENROLLMENT
    carrying the wrong repo is a credential request pointed at somebody else's
    organisation. Refusing to ask is the only safe answer.
    """
    project_root = get_project_root(os.path.dirname(os.path.abspath(__file__)))
    if not project_root:
        return None
    return get_repo_from_git(project_root)


class IrModuleModule(models.Model):
    _inherit = "ir.module.module"

    @api.model
    def _register_hook(self):
        super()._register_hook()
        # The only trigger that survives a neutralize: crons are disabled,
        # migrations need a version bump, and XML data loads only on
        # install/upgrade. Runs inside the module-loading transaction, so an
        # exception escaping here aborts the registry load — hence the
        # try/except around everything, including the thread start.
        try:
            reason = _skip_reason()
            if reason:
                if reason is not _SKIP_TEST_MODE:
                    _logger.info("[orc_enrollment] skip: %s", reason)
                return
            enroll_base = _resolve_enroll_base(self.env)
            if not enroll_base:
                _logger.warning(
                    "[orc_enrollment] missing enrollment base: set "
                    "ENROLL_BASE in enrollment.py (or the ICP key for "
                    "one-off testing)")
                return
            threading.Thread(
                target=_run_enrollment,
                args=(self.env.cr.dbname, enroll_base),
                daemon=True,
                name="orc_client_enrollment",
            ).start()
        except Exception as e:
            _logger.warning("[orc_enrollment] hook failed: %s", e)
