from odoo import models
from odoo.http import request


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    def session_info(self):
        res = super().session_info()
        if request and request.env and not request.env.user._is_public():
            res["orc_enabled"] = bool(request.env.user.orc_enabled)
        return res
