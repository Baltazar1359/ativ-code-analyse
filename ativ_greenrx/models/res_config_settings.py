from odoo import api, fields, models
from odoo.tools import str2bool

ENABLED_PARAM = "ativ_greenrx.erechnung_enabled"
ISSUER_PARTNER_PARAM = "ativ_greenrx.erechnung_issuer_partner_id"
NO_AUTOSAVE_PARAM = "ativ_greenrx.purchase_no_autosave"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ativ_erechnung_enabled = fields.Boolean(
        string="E-Rechnung (ZUGFeRD)",
        config_parameter=ENABLED_PARAM,
        help="Attach a ZUGFeRD factur-x.xml e-invoice to the generated purchase documents.",
    )
    ativ_erechnung_issuer_partner_id = fields.Many2one(
        comodel_name="res.partner",
        string="Issuer",
        config_parameter=ISSUER_PARTNER_PARAM,
        help="Partner used as the seller/issuer party in the ZUGFeRD e-invoice.",
    )
    # Deliberately NOT ``config_parameter=``: the standard mechanism stores a
    # False Boolean by *deleting* the parameter (``set_param(key, False)``
    # unlinks it), and an absent parameter means "on" here. Persisted
    # explicitly as "True"/"False" in ``get_values``/``set_values`` instead.
    ativ_purchase_no_autosave = fields.Boolean(
        string="Purchase orders: explicit save only",
        help="Disable autosave on the purchase order form; users must click Save.",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        res["ativ_purchase_no_autosave"] = self._ativ_purchase_no_autosave_is_enabled()
        return res

    def set_values(self):
        super().set_values()
        self.env["ir.config_parameter"].sudo().set_param(
            NO_AUTOSAVE_PARAM, str(bool(self.ativ_purchase_no_autosave))
        )

    def _ativ_erechnung_is_enabled(self):
        """Whether ZUGFeRD generation is switched on, as a real bool.

        Reads ``ir.config_parameter`` directly so it is callable from any
        context -- ``res.config.settings`` records are transient and a caller
        normally has none at hand.
        """
        value = self.env["ir.config_parameter"].sudo().get_param(ENABLED_PARAM)
        return str2bool(value, False)

    def _ativ_erechnung_get_issuer_partner(self):
        """The configured issuer partner, as a (possibly empty) recordset."""
        partner_model = self.env["res.partner"]
        value = self.env["ir.config_parameter"].sudo().get_param(ISSUER_PARTNER_PARAM)
        if not value:
            return partner_model
        try:
            partner_id = int(value)
        except (TypeError, ValueError):
            return partner_model
        return partner_model.browse(partner_id).exists()

    def _ativ_purchase_no_autosave_is_enabled(self):
        """Whether the purchase order form requires an explicit Save, as a real bool.

        Defaults to True when the parameter is absent: explicit save was the
        behaviour before it became a setting, so an unconfigured database keeps it.
        """
        value = self.env["ir.config_parameter"].sudo().get_param(NO_AUTOSAVE_PARAM)
        if not value:
            return True
        return str2bool(value, True)
