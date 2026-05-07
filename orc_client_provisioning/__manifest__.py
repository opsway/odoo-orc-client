{
    "name": "ORC Client - Provisioning",
    "version": "13.0.2.0.0",
    "summary": (
        "Provision Odoo users into OpsWay ORC with auto-rotated API keys, "
        "header-gated RPC auth, immutable audit logs, and single-click SSO."
    ),
    "description": """
ORC Client - Provisioning (Odoo 13)
====================================

Provisions Odoo users into the OpsWay ORC platform. For each enrolled
user the addon:

- mints an Odoo API key stored on res.users (hashed at rest), pushed
  to ORC encrypted-at-rest via pgcrypto on the ORC side
- rotates the key on a configurable interval (default 30 days)
- authenticates inbound XML-RPC / JSON-RPC calls that carry the
  ``X-ORC-Auth`` header against the per-user key (passwords still work
  for everything else)
- records every ORC RPC call (user, endpoint, status, source IP) in an
  append-only audit log; provisioning lifecycle events go to a second
  append-only log
- exposes a systray button that signs the user in to ORC with no
  second password prompt (server-to-server nonce, one-time, 60 s TTL)

Configuration is via ``ir.config_parameter`` keys. See README.
""",
    "author": "OpsWay",
    "website": "https://opsway.com",
    "license": "LGPL-3",
    "category": "Productivity",
    "depends": ["base", "web", "mail"],
    "external_dependencies": {"python": ["requests", "passlib"]},
    "data": [
        "security/orc_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "data/ir_config_parameter.xml",
        "views/res_users_views.xml",
        "views/orc_audit_log_views.xml",
        "views/orc_api_access_log_views.xml",
        "views/menu.xml",
        "views/assets.xml",
    ],
    "qweb": [
        "static/src/xml/orc_systray.xml",
    ],
    "installable": True,
    "application": False,
}
