"""Phone home to AI Workplace with this build's identifying tuple.

What this addon does
--------------------

On every Odoo.sh registry init (once per worker), POSTs a single
report to the AI Workplace's public webhook::

    POST {WEBHOOK_BASE}/{ORG_ID}/{sha}
    {
        "build_url":   "https://<slug>-<build_id>.dev.odoo.com",
        "stage":       "dev" | "staging" | "production",
        "build_id":    "<digits>",
        "branch_slug": "<slug>"
    }

Workplace stores ``(sha → build_id, dev_url, ssh_target)`` in its
``odoo_sh_builds`` table; the developer-flow agent reads from there
to know *which* dev URL to SSH into for a given commit.

Why this shape (vs the v1 GitHub-PAT approach)
----------------------------------------------

Odoo.sh creates a fresh DB on every "New build" mode push. A PAT
stored in ``ir.config_parameter`` is wiped along with the DB, so v1
silently stopped reporting after the first new build. The current
path:

* **No secret in the addon** — ``ORG_ID`` is a public routing
  identifier; the SHA is public the moment it's pushed.
* **No GitHub token anywhere** — Workplace has its own PAT for the
  SHA-on-repo cross-check on the receiving side.
* **Survives DB resets** — ``ORG_ID`` and ``WEBHOOK_BASE`` are
  constants in this source file, part of every fresh build's
  filesystem.
* **Robust to spoofing** — Workplace validates the SHA exists on the
  org's known repo, structurally checks the ``build_url`` belongs to
  ``.odoo.com``, and the agent re-verifies ``git rev-parse HEAD`` on
  the dev server before acting on the reported ``ssh_target``.
"""
import logging
import os
import re
import subprocess
import threading

import requests

from odoo import api, models, SUPERUSER_ID
from odoo.modules.registry import Registry
from odoo.tools import config

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# === CUSTOMER CONFIGURATION ===
#
# Edit these two values for your deployment, then commit. Both are
# PUBLIC identifiers — NOT secrets — so committing them is safe.
#
#   ORG_ID        Your organisation's UUID in AI Workplace. Visible
#                 in the Workplace admin UI on the org details page.
#   WEBHOOK_BASE  Your Workplace deployment's webhook root, e.g.
#                 ``https://orc.example.com/webhook/odoo-sh/build-ready``.
#
# Both values can be overridden at runtime by setting the matching
# ``ir.config_parameter`` keys (see ``res_config_settings.py``) — useful
# for staging tests without forking. The hard-coded defaults below
# win whenever the ICP entries are empty or missing.
# ---------------------------------------------------------------------------
ORG_ID = ""               # e.g. "11111111-2222-3333-4444-555555555555"
WEBHOOK_BASE = ""         # e.g. "https://orc.opsway.com/webhook/odoo-sh/build-ready"
# ---------------------------------------------------------------------------

_PARAM_ORG_ID = "orc_client_build_reporter.org_id"
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


def get_commit_sha(addon_dir):
    """Reads ``git rev-parse HEAD`` from the addon's own checkout."""
    try:
        return subprocess.check_output(
            ["git", "-C", addon_dir, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
    except Exception:
        return None


def parse_dev_url(build_url):
    """Returns (branch_slug, build_id) if `build_url` is a canonical
    Odoo.sh dev hostname, else None.

    >>> parse_dev_url("https://acme-32258372.dev.odoo.com")
    ('acme', '32258372')
    >>> parse_dev_url("https://evil.attacker.com")
    """
    try:
        from urllib.parse import urlparse
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


def _resolve_config(env):
    """Return (org_id, webhook_base) using ICP overrides if set, else
    the in-source constants. ``None`` for either means "not configured"."""
    ICP = env["ir.config_parameter"].sudo()
    org_id = (ICP.get_param(_PARAM_ORG_ID) or "").strip() or (ORG_ID or "").strip()
    webhook_base = (
        ICP.get_param(_PARAM_WEBHOOK_BASE) or ""
    ).strip() or (WEBHOOK_BASE or "").strip()
    return (org_id or None, webhook_base or None)


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

        # --- 2. Derive SHA from the addon's own checkout -----------------
        addon_dir = os.path.dirname(os.path.abspath(__file__))
        sha = get_commit_sha(addon_dir)
        if not sha:
            _logger.warning("[orc_build_reporter] cannot derive sha")
            return

        # --- 3. Stage detection ------------------------------------------
        stage = get_stage()

        # --- 4. Config + debounce ----------------------------------------
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            org_id, webhook_base = _resolve_config(env)
            if not org_id or not webhook_base:
                _logger.warning(
                    "[orc_build_reporter] missing config: set ORG_ID and "
                    "WEBHOOK_BASE in build_reporter.py (or the matching "
                    "ICP keys for one-off testing)",
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
            ICP.set_param(_PARAM_LAST_REPORT, current_key)

        # --- 5. POST -----------------------------------------------------
        url = f"{webhook_base.rstrip('/')}/{org_id}/{sha}"
        body = {
            "build_url": build_url,
            "stage": stage,
            "build_id": build_id,
            "branch_slug": branch_slug,
        }
        _logger.info(
            "[orc_build_reporter] reporting org=%s sha=%s build_id=%s stage=%s",
            org_id, sha[:8], build_id, stage,
        )
        r = requests.post(
            url, json=body, timeout=10,
            headers={
                "User-Agent": "orc-client-build-reporter/1.0",
                "Accept": "application/json",
            },
        )
        r.raise_for_status()
        _logger.info(
            "[orc_build_reporter] reported: %s",
            (r.text or "").strip()[:200],
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
