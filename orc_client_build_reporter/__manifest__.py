{
    "name": "AI Workplace — Build Reporter",
    "version": "19.0.1.0.0",
    "summary": (
        "Phones home to AI Workplace on every Odoo.sh registry init so the"
        " developer-flow agent can resolve `(commit sha → build_id, dev"
        " URL, ssh target)` without any GitHub token."
    ),
    "description": """
AI Workplace — Build Reporter
==============================

A drop-in Odoo.sh addon that POSTs each build's identifying tuple to
the AI Workplace webhook on every registry init. The Workplace agent
reads from its own ``odoo_sh_builds`` PG table to learn which dev URL
to SSH into for a given commit — no GitHub commit-status polling, no
PAT inside Odoo, no per-tenant secret in the repo.

What gets sent
--------------

::

    POST {WEBHOOK_BASE}/{sha}
    {
        "build_url":   "https://<slug>-<build_id>.dev.odoo.com",
        "stage":       "dev" | "staging" | "production",
        "build_id":    "<digits>",
        "branch_slug": "<slug>",
        "repo":        "<owner>/<name>"
    }

All five body fields are auto-derived on the dev server. The
receiving Workplace resolves the report's owning organisation by
matching ``repo`` against its stored ``organizations.github_repo``.

Configuration
-------------

The default ``WEBHOOK_BASE`` in ``models/build_reporter.py`` points
at OpsWay's production AI Workplace. If you self-host, override:

::

    WEBHOOK_BASE = "https://your-workplace.example.com/webhook/odoo-sh/build-ready"

Or set the ICP key ``orc_client_build_reporter.webhook_base`` at
runtime. ICP wins when set; in-source constant is the fallback.
Commit the constant for durability — Odoo.sh "New build" mode wipes
ICP between builds.

Skip conditions
---------------

Quiet exits (one log line at most) when:

1. Odoo is in test mode (``test_enable`` / ``test_file``).
2. No build_id derivable from ``ODOO_BUILD_URL`` or ``cr.dbname``.
3. Current commit SHA cannot be derived from the addon's checkout.
4. Neither in-source constants nor ICP overrides set.
5. The git origin URL doesn't resolve to a GitHub ``owner/repo``
   shape (self-hosted GitLab and similar — AI Workplace only
   handles GitHub today).
6. The current ``{sha}:{build_id}:{stage}`` triple was already
   reported (``last_report_key`` debounce across Odoo workers).
""",
    "author": "OpsWay",
    "website": "https://opsway.com",
    "license": "LGPL-3",
    "category": "Productivity",
    "depends": ["base"],
    "external_dependencies": {"python": ["requests"]},
    "auto_install": True,
    "data": ["views/res_config_settings_views.xml"],
    "installable": True,
    "application": False,
}
