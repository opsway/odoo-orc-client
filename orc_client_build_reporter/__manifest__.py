{
    "name": "AI Workplace — Build Reporter",
    "version": "18.0.1.0.0",
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

    POST {WEBHOOK_BASE}/{ORG_ID}/{sha}
    {
        "build_url":   "https://<slug>-<build_id>.dev.odoo.com",
        "stage":       "dev" | "staging" | "production",
        "build_id":    "<digits>",
        "branch_slug": "<slug>"
    }

``sha``, ``build_id``, ``branch_slug`` and ``stage`` are all derived
on the dev server. The only customer-set values are ``ORG_ID`` and
``WEBHOOK_BASE`` — both PUBLIC identifiers, safe to commit.

Configuration
-------------

Edit the constants in ``models/build_reporter.py``:

::

    ORG_ID = "11111111-2222-3333-4444-555555555555"
    WEBHOOK_BASE = "https://orc.example.com/webhook/odoo-sh/build-ready"

Or override at runtime via ``ir.config_parameter``:

::

    orc_client_build_reporter.org_id        ← same as ORG_ID
    orc_client_build_reporter.webhook_base  ← same as WEBHOOK_BASE

ICP wins when set; in-source constants are the fallback. Commit the
constants for durability — Odoo.sh "New build" mode wipes ICP between
builds.

Skip conditions
---------------

Quiet exits (one log line at most) when:

1. Odoo is in test mode (``test_enable`` / ``test_file``).
2. No build_id derivable from ``ODOO_BUILD_URL`` or ``cr.dbname``.
3. Current commit SHA cannot be derived from the addon's checkout.
4. Neither in-source constants nor ICP overrides set.
5. The current ``{sha}:{build_id}:{stage}`` triple was already
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
