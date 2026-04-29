{
    "name": "Odoo Resolution Center — Provisioning",
    "version": "18.0.1.8.0",
    "summary": "Provision Odoo users into the Odoo Resolution Center with auto-rotated API keys and single-click SSO.",
    "description": """
Odoo Resolution Center — Provisioning
=====================================

Phase 1 addon. Lets an Odoo admin pick which users get access to the
Odoo Resolution Center. For each enrolled user the addon:

- creates a dedicated Odoo API key scoped "ORC (auto-managed)"
- ships the key to the Odoo Resolution Center (encrypted at rest via pgcrypto)
- rotates the key on a configurable interval (default 30 days)
- exposes a systray button that signs the user in to the Odoo Resolution
  Center with no second password prompt (server-to-server nonce exchange,
  one-time, 60-second TTL)

Configuration is via ``ir.config_parameter`` keys. See the repo README.
""",
    "author": "OpsWay",
    "website": "https://opsway.com",
    "license": "LGPL-3",
    "category": "Productivity",
    "depends": ["base", "web", "mail"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/orc_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "data/ir_config_parameter.xml",
        "views/res_users_views.xml",
        "views/orc_audit_log_views.xml",
        "views/menu.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "orc_client_provisioning/static/src/js/orc_systray.js",
            "orc_client_provisioning/static/src/js/orc_systray.xml",
            "orc_client_provisioning/static/src/scss/orc_systray.scss",
        ],
    },
    "installable": True,
    "application": False,
}
