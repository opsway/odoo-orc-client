"""Optional runtime overrides for ORG_ID and WEBHOOK_BASE.

The in-source constants in ``build_reporter.py`` are the durable
source of truth (they survive Odoo.sh "New build" DB wipes). These
ICP-backed fields let an operator override at runtime without
forking — useful for staging tests. ICP wins when set; empty means
"use the in-source default".
"""
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    orc_build_reporter_org_id = fields.Char(
        string="AI Workplace org_id",
        config_parameter="orc_client_build_reporter.org_id",
        help=(
            "Organisation UUID in AI Workplace. PUBLIC identifier — not a"
            " secret. Leave empty to use the in-source default."
        ),
    )
    orc_build_reporter_webhook_base = fields.Char(
        string="AI Workplace webhook base URL",
        config_parameter="orc_client_build_reporter.webhook_base",
        help=(
            "Your AI Workplace deployment's webhook root, e.g."
            " `https://orc.example.com/webhook/odoo-sh/build-ready`."
            " Leave empty to use the in-source default."
        ),
    )
