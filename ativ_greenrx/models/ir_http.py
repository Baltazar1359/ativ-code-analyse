from odoo import models


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    def session_info(self):
        """Expose the "explicit save on purchase orders" toggle to the web client.

        Read by ``static/src/purchase_order_no_autosave.js`` via ``@web/session``.
        """
        result = super().session_info()
        result["ativ_purchase_no_autosave"] = self.env[
            "res.config.settings"
        ]._ativ_purchase_no_autosave_is_enabled()
        return result
