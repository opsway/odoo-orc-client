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


def _run_reporter(dbname):
    """The whole thing is wrapped in a try/except that never re-raises.
    A failure here must never block Odoo startup."""
    try:
        if config.get("test_enable") or config.get("test_file"):
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

        # --- 4. Config + debounce check ----------------------------------
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            webhook_base = _resolve_webhook_base(env)
            if not webhook_base:
                _logger.warning(
                    "[orc_build_reporter] missing webhook base: set "
                    "WEBHOOK_BASE in build_reporter.py (or the ICP key "
                    "for one-off testing)",
                )
                return

            ICP = env["ir.config_parameter"].sudo()
            # Debounce key spans every field that — when changed —
            # legitimately re-warrants a report: sha, build_id, stage.
            current_key = f"{sha}:{build_id}:{stage}"
            if ICP.get_param(_PARAM_LAST_REPORT) == current_key:
                _logger.info(
                    "[orc_build_reporter] skip: %s already reported "
                    "(clear ICP %s to force re-post)",
                    current_key, _PARAM_LAST_REPORT,
                )
                return
            # The debounce key is stamped only after the POST succeeds
            # (step 6). Stamping here would commit on cursor exit before
            # the webhook call, so a timeout/non-2xx would permanently
            # suppress the retry-on-next-restart path.

        # --- 5. POST -----------------------------------------------------
        url = f"{webhook_base.rstrip('/')}/{sha}"
        body = {
            "build_url": build_url,
            "stage": stage,
            "build_id": build_id,
            "branch_slug": branch_slug,
            "repo": repo,
        }
        _logger.info(
            "[orc_build_reporter] reporting repo=%s sha=%s build_id=%s stage=%s",
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

        # --- 6. Debounce stamp (only on confirmed success) ---------------
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
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
        threading.Thread(
            target=_run_reporter,
            args=(self.env.cr.dbname,),
            daemon=True,
            name="orc_client_build_reporter",
        ).start()
