"""Optional runtime override for WEBHOOK_BASE.

The in-source constant in ``build_reporter.py`` is the durable source
of truth (it survives Odoo.sh "New build" DB wipes). This ICP-backed
field lets an operator override at runtime without forking — useful
for staging tests. ICP wins when set; empty means "use the in-source
default".
"""
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    orc_build_reporter_webhook_base = fields.Char(
        string="AI Workplace webhook base URL",
        config_parameter="orc_client_build_reporter.webhook_base",
        help=(
            "Your AI Workplace deployment's webhook root, e.g."
            " `https://help.opsway.com/webhook/odoo-sh/build-ready`."
            " Leave empty to use the in-source default."
        ),
    )
