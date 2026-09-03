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
_PARAM_ENROLL_DONE = "orc_client_build_reporter.enroll_done_key"

# Distinct from the reporter's key: the two guard different work and must not
# exclude one another.
_ADVISORY_LOCK_KEY = 1108763548

_ATTEMPTS = 3
_BACKOFF_SECONDS = (2, 5)
# Under AI Workplace's nginx cap for this location; a longer client timeout
# would just wait past the point the server stopped listening.
_POST_TIMEOUT = 10

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


def _try_lock(cr):
    """Take enrollment's transaction-scoped advisory lock, or give up.

    Non-blocking: the holder is writing the very secret this worker would have
    written, and commits within microseconds.
    """
    cr.execute("SELECT pg_try_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
    return bool(cr.fetchone()[0])


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


def _claim_secret(env, cfg):
    """The committed preimage for this build, generating one only if absent.

    Claim-once-and-re-read is what makes a committed secret safe across
    workers: a worker that loses the lock re-reads the winner's value instead
    of overwriting it, so every worker publishes and submits the same ``S``.
    """
    existing = cfg.get(_PARAM_ENROLL_SECRET)
    if existing:
        return existing
    if not _try_lock(env.cr):
        # The holder is mid-write and commits imminently. Nothing to wait for;
        # the retry below re-reads.
        return None
    # Re-read UNDER the lock: another worker may have committed between our
    # first read and acquiring it.
    _flush_params(env)
    fresh = _params(env.cr, [_PARAM_ENROLL_SECRET]).get(_PARAM_ENROLL_SECRET)
    if fresh:
        return fresh
    secret = secrets.token_hex(32)
    env["ir.config_parameter"].sudo().set_param(_PARAM_ENROLL_SECRET, secret)
    return secret


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
    # `orc.rotation_days` is NOT in neutralize.sql's DELETE list, so it
    # normally survives; restore the documented default only if a restore
    # dropped it too.
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
        return "spent", None
    detail = (r.text or "").strip()[:200]
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
            secret = _claim_secret(env, cfg)
            # `orc.endpoint_url` is the AI Workplace ORIGIN, derived from the
            # enrollment base we are about to POST to, so the addon never has
            # to be told twice where its Workplace lives.
            #
            # Parsed rather than string-split: splitting on "/webhook/" quietly
            # returns the WHOLE url for a base that does not contain it, which
            # would write a path into `orc.endpoint_url` and break every later
            # API call with a 404 that looks like a server problem.
            endpoint_url = _origin_of(enroll_base)
            if not endpoint_url:
                _logger.warning(
                    "[orc_enrollment] enrollment base %r has no usable origin",
                    enroll_base)
                return

        if not secret:
            _logger.info("[orc_enrollment] another worker is claiming the "
                         "challenge; leaving it to them")
            return

        # ---- 2. Prove it -----------------------------------------------
        body = {
            "repo": _repo_for(),
            "branch_slug": branch_slug,
            "build_id": build_id,
            "proof": secret,
        }
        if not body["repo"]:
            _logger.warning("[orc_enrollment] cannot derive repo from the "
                            "project's origin URL; not enrolling")
            return

        minted = None
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                outcome, data = _post_enrollment(enroll_base, body, attempt)
            except Exception as e:
                outcome, data = "retry", None
                _logger.info("[orc_enrollment] attempt %s failed: %s", attempt, e)
            if outcome == "minted":
                minted = data
                break
            if outcome in ("spent", "stop"):
                if outcome == "spent":
                    _logger.info(
                        "[orc_enrollment] challenge already spent — another "
                        "worker of this build enrolled it")
                return
            if attempt < _ATTEMPTS:
                # Bounded, because "retry on the next restart" is too weak
                # here: Workplace calls back seconds after registry init, and
                # if the HTTP frontend is not serving yet the challenge fetch
                # fails. Without a retry the build stays dead until somebody
                # restarts it by hand — reintroducing the manual step this
                # feature removes.
                time.sleep(_BACKOFF_SECONDS[attempt - 1])

        if not minted:
            _logger.warning("[orc_enrollment] gave up after %s attempts; the "
                            "next restart will try again", _ATTEMPTS)
            return

        # ---- 3. Apply. Second short transaction. -------------------------
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            _apply(env, dict(minted, endpoint_url=endpoint_url))
            env["ir.config_parameter"].sudo().set_param(
                _PARAM_ENROLL_DONE, done_key)
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
