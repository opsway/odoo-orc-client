{
    "name": "ORC Client — Provisioning",
    "version": "18.0.1.2.0",
    "summary": "Provision Odoo users into OpsWay ORC with auto-rotated API keys and single-click SSO.",
    "description": """
ORC Client — Provisioning
=========================

Phase 1 addon. Lets an Odoo admin pick which users get access to the
OpsWay ORC platform. For each enrolled user the addon:

- creates a dedicated Odoo API key scoped "ORC (auto-managed)"
- ships the key to ORC (encrypted at rest via pgcrypto)
- rotates the key on a configurable interval (default 30 days)
- exposes a systray "Open ORC" button that signs the user in to ORC
  with no second password prompt (server-to-server nonce exchange,
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
