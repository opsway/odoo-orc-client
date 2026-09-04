"""Phone home to AI Workplace with this build's identifying tuple.

What this addon does
--------------------

On every Odoo.sh registry init (once per worker), POSTs a single
report to the AI Workplace's public webhook::

    POST {WEBHOOK_BASE}/{sha}
    {
        "build_url":   "https://<slug>-<build_id>.dev.odoo.com",
        "stage":       "dev" | "staging" | "production",
        "build_id":    "<digits>",
        "branch_slug": "<slug>",
        "repo":        "<owner>/<name>"
    }

Workplace routes the report to the right organisation by matching
``repo`` against its stored ``organizations.github_repo``. It stores
``(sha → build_id, dev_url, ssh_target)`` so the developer-flow
agent can resolve "which dev URL do I SSH into for this commit?"
from PG without any GitHub round-trip.

Why this shape (vs the v1 GitHub-PAT approach)
----------------------------------------------

Odoo.sh creates a fresh DB on every "New build" mode push. A PAT
stored in ``ir.config_parameter`` is wiped along with the DB, so v1
silently stopped reporting after the first new build. The current
path:

* **No secret in the addon** — the SHA is public the moment it's
  pushed; ``repo`` is derived from the customer project's git origin
  (resolved up through any submodule layer — see
  ``get_project_root``) and is already visible to anyone with repo
  read access.
* **No GitHub token anywhere** — Workplace has its own PAT for the
  SHA-on-repo cross-check on the receiving side.
* **Survives DB resets** — ``WEBHOOK_BASE`` is a constant in this
  source file, part of every fresh build's filesystem.
* **Robust to spoofing** — Workplace validates the SHA exists on the
  reported repo, structurally checks the ``build_url`` is a
  ``.odoo.com`` host, and the agent re-verifies ``git rev-parse
  HEAD`` on the dev server before acting on the reported
  ``ssh_target``.

Why the reporter does not run under ``--stop-after-init``
---------------------------------------------------------

The POST runs in a daemon thread so that a slow or unreachable
webhook cannot hold up startup. That is safe only in a process that
intends to keep running.

``ThreadedServer.run()`` calls ``preload_registries()`` — which is
where ``_register_hook``, and therefore this thread's ``start()``,
happen — and then, when ``--stop-after-init`` is set, ``self.stop()``
immediately. ``stop()`` joins only **non-daemon** threads before
calling ``sql_db.close_all()``, so a pooled connection this thread
has just checked out gets closed underneath it mid-query.
``sql_db.execute`` logs that at ERROR *before* re-raising, one frame
below this addon's ``try/except`` — the ERROR therefore cannot be
suppressed here, and Odoo.sh reds the whole build over it even though
the build itself succeeded.

``_skip_reason`` therefore refuses to run under ``--stop-after-init``
at all. Nothing is lost: on Odoo.sh the serving process starts moments
later and runs ``_register_hook`` again with no shutdown race — that
is where every successful report has always come from anyway.

How the debounce holds across workers
-------------------------------------

Every worker that loads this registry runs the hook, so several
threads can be deriving the same ``{sha, build_id, stage}`` at once.
Reading ``last_report_key``, comparing it, and stamping it after the
POST is a read-check-act sequence: without serialisation each worker
reads the same stale value and each one posts, which is not the
"debounce across Odoo workers" this addon advertises.

The claim, the POST and the stamp therefore share **one transaction**,
opened by taking a *transaction-scoped* Postgres advisory lock
(``_try_lock``). Losers exit immediately — the winner is posting that
exact tuple right now.

Transaction scope is what makes this safe rather than merely mutually
exclusive. A persisted "claim" row written before the POST would have
to be cleared afterwards by code a ``SIGKILL`` never reaches, and a
stale claim suppresses *every* future report — the permanent-
suppression bug this addon already fixed once by moving the stamp
after the POST. An advisory lock needs no cleanup: the commit drops
it, any exception rolls it back, and a killed backend drops it when
the connection dies. In each of those cases the stamp is absent too,
so the next registry load retries.

The cost is one connection idle-in-transaction for at most the POST
timeout. It holds only the advisory lock and ``ACCESS SHARE`` on
``ir_config_parameter``, and the ``--stop-after-init`` guard keeps it
out of the build phase, where schema locks are held.
"""
import logging
import os
import re
import subprocess
import threading
from urllib.parse import urlparse

import requests

from odoo import api, models, SUPERUSER_ID
from odoo.modules.registry import Registry
from odoo.tools import config

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# === CUSTOMER CONFIGURATION ===
#
# Edit this value for your deployment, then commit. It is a PUBLIC
# URL — NOT a secret — so committing is safe.
#
#   WEBHOOK_BASE  Your AI Workplace deployment's webhook root, e.g.
#                 ``https://help.opsway.com/webhook/odoo-sh/build-ready``.
#
# The value can be overridden at runtime by setting the matching
# ``ir.config_parameter`` key (see ``res_config_settings.py``) — useful
# for staging tests without forking. The hard-coded default below
# wins whenever the ICP entry is empty.
# ---------------------------------------------------------------------------
WEBHOOK_BASE = "https://help.opsway.com/webhook/odoo-sh/build-ready"
# ---------------------------------------------------------------------------

_PARAM_WEBHOOK_BASE = "orc_client_build_reporter.webhook_base"
_PARAM_LAST_REPORT = "orc_client_build_reporter.last_report_key"

# Serialises the claim/POST/stamp sequence across the workers of one
# database. Any stable value works as long as every worker agrees on it;
# this one is `zlib.crc32(b"orc_client_build_reporter")`, hard-coded so
# it cannot drift with the hash implementation.
_ADVISORY_LOCK_KEY = 1108763547

# Odoo.sh sets these env vars on every build container. We extract
# build_id and branch_slug from ODOO_BUILD_URL when possible; fall
# back to dbname parsing if the env var is absent (local installs).
_ENV_BUILD_URL = "ODOO_BUILD_URL"
_ENV_STAGE = "ODOO_STAGE"

_DEV_HOST_RE = re.compile(
    r"^(?P<slug>[a-z0-9][a-z0-9-]+)-(?P<build_id>\d+)\.dev\.odoo\.com$"
)
_VALID_STAGES = ("dev", "staging", "production")
# `git@github.com:owner/repo.git` or `https://github.com/owner/repo[.git]`.
_GH_URL_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")


def get_build_id(dbname):
    """Extract the numeric trailing segment of an Odoo.sh dbname.

    >>> get_build_id("pg-group-stage-25407779")
    '25407779'
    >>> get_build_id("opsway-stage-30699587")
    '30699587'
    >>> get_build_id("odoo")  # local
    """
    parts = dbname.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[1]
    return None


def get_commit_sha(repo_dir):
    """Reads ``git rev-parse HEAD`` from the given working tree."""
    try:
        return subprocess.check_output(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
    except Exception:
        return None


def get_repo_from_git(repo_dir):
    """Parse ``owner/repo`` from the working tree's origin URL.

    Supports both ``git@github.com:owner/repo.git`` and
    ``https://github.com/owner/repo[.git]``. Returns None on a
    non-GitHub origin (e.g. a self-hosted GitLab) — AI Workplace
    only handles GitHub-hosted projects today.
    """
    try:
        url = subprocess.check_output(
            ["git", "-C", repo_dir, "config", "--get", "remote.origin.url"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
        m = _GH_URL_RE.search(url)
        return f"{m.group(1)}/{m.group(2)}" if m else None
    except Exception:
        return None


def get_project_root(start_dir):
    """Resolve the *customer project* working-tree root from any path
    inside it.

    The build we must report is the customer's Odoo.sh project — its
    repo and the commit it pushed. But the addon can physically live
    in several places, and only some of them share the project's repo:

    * committed straight into the customer repo (``addons/…`` or the
      repo root) — ``start_dir`` already belongs to that repo;
    * pulled in as a git submodule (``submodules/odoo-orc-client/…``)
      — ``start_dir`` belongs to the *submodule*, whose origin/HEAD
      are the addon's own (``opsway/odoo-orc-client`` at the pinned
      sub-SHA), NOT the customer's;
    * a submodule nested inside another submodule.

    Reading ``git config remote.origin.url`` / ``rev-parse HEAD`` from
    the addon dir is therefore correct only for the first layout; for
    a submodule it reports the wrong repo and SHA, and Workplace
    rejects the webhook (``no org configured for repo
    opsway/odoo-orc-client``).

    Walk up the superproject chain to the outermost working tree, then
    normalise to its toplevel — giving the customer repo + commit in
    every layout. Returns None when ``start_dir`` is not inside a git
    repo at all (addon copied into a plain addons path); callers fall
    back to ``start_dir`` and skip-on-no-repo as before.
    """
    cur = start_dir
    try:
        # `--show-superproject-working-tree` prints the parent project's
        # path when `cur` is inside a submodule, and nothing otherwise.
        # Loop to climb out of submodules nested in submodules.
        while True:
            sup = subprocess.check_output(
                ["git", "-C", cur, "rev-parse",
                 "--show-superproject-working-tree"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode().strip()
            if not sup:
                break
            cur = sup
        top = subprocess.check_output(
            ["git", "-C", cur, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
        return top or None
    except Exception:
        return None


def parse_dev_url(build_url):
    """Returns (branch_slug, build_id) if `build_url` is a canonical
    Odoo.sh dev hostname, else None."""
    try:
        parsed = urlparse(build_url)
    except Exception:
        return None
    if not parsed.hostname:
        return None
    m = _DEV_HOST_RE.match(parsed.hostname)
    if not m:
        return None
    return m.group("slug"), m.group("build_id")


def get_stage():
    """Returns 'dev' | 'staging' | 'production'. Defaults to 'dev'
    when the env var is unset (e.g. local installs)."""
    stage = (os.environ.get(_ENV_STAGE) or "").strip().lower()
    if stage in _VALID_STAGES:
        return stage
    return "dev"


def _resolve_webhook_base(env):
    """ICP value wins if set; in-source constant is the fallback."""
    ICP = env["ir.config_parameter"].sudo()
    icp_value = (ICP.get_param(_PARAM_WEBHOOK_BASE) or "").strip()
    return icp_value or (WEBHOOK_BASE or "").strip() or None


_SKIP_TEST_MODE = "test mode"
_SKIP_STOP_AFTER_INIT = (
    "--stop-after-init is set, so this process exits as soon as the "
    "registry is loaded and ThreadedServer.stop() closes the connection "
    "pool without waiting for daemon threads; the serving process that "
    "starts next reports instead"
)


def _skip_reason():
    """Why the reporter must not run in this process, or None to proceed.

    ``--stop-after-init`` is Odoo.sh's build phase (and any local
    ``-u``/``-i`` run). Reporting from there is not merely unnecessary,
    it is harmful — see the module docstring.
    """
    if config.get("test_enable") or config.get("test_file"):
        return _SKIP_TEST_MODE
    if config.get("stop_after_init"):
        return _SKIP_STOP_AFTER_INIT
    return None


def _try_lock(cr):
    """Take the reporter's transaction-scoped advisory lock, or give up.

    Non-blocking on purpose: a worker that cannot get the lock has
    nothing useful to wait for, because the holder is posting the very
    tuple this worker would have posted.
    """
    cr.execute("SELECT pg_try_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
    return bool(cr.fetchone()[0])


def _last_report_key(env):
    """Read the debounce key straight off `env`'s transaction.

    Deliberately not ``ICP.get_param``: that is ``@ormcache``'d on the
    key, so it can hand back a value this process cached before another
    worker stamped a new one — exactly the staleness the lock exists to
    eliminate. Flush first for the same reason core's own ``_get_param``
    does: an uncommitted ``set_param`` in this transaction must be
    visible to the raw SELECT.
    """
    ICP = env["ir.config_parameter"].sudo()
    ICP.flush(["key", "value"])
    env.cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s",
        (_PARAM_LAST_REPORT,),
    )
    row = env.cr.fetchone()
    return row[0] if row else None


def _run_reporter(dbname, webhook_base):
    """Derive this build's tuple, POST it, then stamp the debounce key.

    Runs in a daemon thread. ``webhook_base`` comes from the caller so
    that a process with no webhook configured never spawns the thread
    at all.

    The whole thing is wrapped in a try/except that never re-raises.
    A failure here must never block Odoo startup.
    """
    try:
        # Belt-and-braces: `_register_hook` already refuses to spawn this
        # thread under these conditions. Repeated here so a direct call
        # (tests, `odoo shell`) obeys them too.
        if _skip_reason():
            return

        _logger.info("[orc_build_reporter] hook fired (dbname=%s)", dbname)

        # --- 1. Derive build_id and branch_slug --------------------------
        # Prefer ODOO_BUILD_URL (canonical on Odoo.sh); fall back to dbname.
        env_build_url = os.environ.get(_ENV_BUILD_URL) or ""
        parsed = parse_dev_url(env_build_url) if env_build_url else None
        if parsed:
            branch_slug, build_id = parsed
            build_url = env_build_url
        else:
            build_id = get_build_id(dbname)
            if not build_id:
                _logger.info(
                    "[orc_build_reporter] skip: no build_id derivable from "
                    "ODOO_BUILD_URL or dbname (not on Odoo.sh?)",
                )
                return
            branch_slug = dbname.rsplit("-", 1)[0]
            build_url = f"https://{branch_slug}-{build_id}.dev.odoo.com"

        # --- 2. Derive SHA and repo from the customer project root -------
        # NOT the addon's own dir: when the addon is vendored as a git
        # submodule, that dir's origin/HEAD are opsway/odoo-orc-client at
        # the pinned sub-SHA, so the report would carry the wrong repo
        # and commit. get_project_root climbs out of any submodule layer
        # to the outermost working tree (the customer repo Odoo.sh built).
        addon_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = get_project_root(addon_dir) or addon_dir
        if project_root != addon_dir:
            _logger.info(
                "[orc_build_reporter] resolved project root %s "
                "(addon at %s)", project_root, addon_dir,
            )
        sha = get_commit_sha(project_root)
        if not sha:
            _logger.warning("[orc_build_reporter] cannot derive sha")
            return
        repo = get_repo_from_git(project_root)
        if not repo:
            _logger.warning(
                "[orc_build_reporter] cannot derive repo from origin URL "
                "(not a GitHub remote?)",
            )
            return

        # --- 3. Stage detection ------------------------------------------
        stage = get_stage()

        if not webhook_base:
            _logger.warning(
                "[orc_build_reporter] missing webhook base: set "
                "WEBHOOK_BASE in build_reporter.py (or the ICP key "
                "for one-off testing)",
            )
            return

        # Debounce key spans every field that — when changed —
        # legitimately re-warrants a report: sha, build_id, stage.
        current_key = f"{sha}:{build_id}:{stage}"
        url = f"{webhook_base.rstrip('/')}/{sha}"
        body = {
            "build_url": build_url,
            "stage": stage,
            "build_id": build_id,
            "branch_slug": branch_slug,
            "repo": repo,
        }

        # --- 4-6. Claim, POST and stamp, in one transaction --------------
        # See "How the debounce holds across workers" in the module
        # docstring. Committing on clean exit publishes the stamp and
        # drops the lock together; every failure path — exception here,
        # or the backend dying mid-POST — rolls both back, so the next
        # registry load retries rather than being suppressed forever.
        with Registry(dbname).cursor() as cr:
            if not _try_lock(cr):
                _logger.info(
                    "[orc_build_reporter] skip: another worker is "
                    "reporting %s right now", current_key,
                )
                return

            env = api.Environment(cr, SUPERUSER_ID, {})
            # Re-read under the lock: a worker that queued behind the
            # winner must see what the winner stamped.
            if _last_report_key(env) == current_key:
                _logger.info(
                    "[orc_build_reporter] skip: %s already reported "
                    "(clear ICP %s to force re-post)",
                    current_key, _PARAM_LAST_REPORT,
                )
                return

            _logger.info(
                "[orc_build_reporter] reporting repo=%s sha=%s "
                "build_id=%s stage=%s",
                repo, sha[:8], build_id, stage,
            )
            r = requests.post(
                url, json=body, timeout=10,
                headers={
                    "User-Agent": "orc-client-build-reporter/1.1",
                    "Accept": "application/json",
                },
            )
            r.raise_for_status()
            _logger.info(
                "[orc_build_reporter] reported: %s",
                (r.text or "").strip()[:200],
            )

            # Stamped only now: before the POST it would let a
            # timeout/non-2xx suppress the retry-on-next-restart path.
            env["ir.config_parameter"].sudo().set_param(
                _PARAM_LAST_REPORT, current_key,
            )
    except Exception as e:
        _logger.warning("[orc_build_reporter] failed: %s", e)


class IrModuleModule(models.Model):
    _inherit = "ir.module.module"

    @api.model
    def _register_hook(self):
        super()._register_hook()
        # This runs inside the module-loading transaction (loading.py
        # STEP 9), so an exception escaping here aborts the registry
        # load. Reporting is best-effort and must never do that.
        try:
            reason = _skip_reason()
            if reason:
                # Test mode stays silent — it would fire on every CI run.
                # The build-phase skip is logged once so `update.log`
                # carries evidence that the guard is in place.
                if reason is not _SKIP_TEST_MODE:
                    _logger.info("[orc_build_reporter] skip: %s", reason)
                return
            webhook_base = _resolve_webhook_base(self.env)
            if not webhook_base:
                _logger.warning(
                    "[orc_build_reporter] missing webhook base: set "
                    "WEBHOOK_BASE in build_reporter.py (or the ICP key "
                    "for one-off testing)",
                )
                return
            threading.Thread(
                target=_run_reporter,
                args=(self.env.cr.dbname, webhook_base),
                daemon=True,
                name="orc_client_build_reporter",
            ).start()
        except Exception as e:
            _logger.warning("[orc_build_reporter] hook failed: %s", e)
