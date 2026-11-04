"""Tests of the "Purchase orders: explicit save only" setting.

The setting defaults to on when its parameter is absent: explicit save was live
before it became switchable, and installing this module must not turn it off.
That default is also why the field is persisted by hand instead of through
``config_parameter=``: the standard path stores a False Boolean by deleting the
parameter, which would read back as "on". ``test_switching_off_persists`` pins
exactly that.
"""

from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.ativ_greenrx.models.res_config_settings import NO_AUTOSAVE_PARAM


@tagged("post_install", "-at_install")
class TestPurchaseNoAutosaveSetting(TransactionCase):

    def setUp(self):
        super().setUp()
        self.config = self.env["ir.config_parameter"].sudo()
        self.config.search([("key", "=", NO_AUTOSAVE_PARAM)]).unlink()

    def test_default_on_when_param_absent(self):
        self.assertFalse(self.config.get_param(NO_AUTOSAVE_PARAM))
        self.assertIs(
            self.env["res.config.settings"]._ativ_purchase_no_autosave_is_enabled(), True
        )
        settings = self.env["res.config.settings"].create({})
        self.assertTrue(settings.ativ_purchase_no_autosave)

    def test_off_when_param_false(self):
        self.config.set_param(NO_AUTOSAVE_PARAM, "False")
        self.assertIs(
            self.env["res.config.settings"]._ativ_purchase_no_autosave_is_enabled(), False
        )
        settings = self.env["res.config.settings"].create({})
        self.assertFalse(settings.ativ_purchase_no_autosave)

    def test_switching_off_persists(self):
        settings = self.env["res.config.settings"].create({})
        settings.ativ_purchase_no_autosave = False
        settings.set_values()
        self.assertEqual(self.config.get_param(NO_AUTOSAVE_PARAM), "False")
        self.assertFalse(self.env["res.config.settings"].create({}).ativ_purchase_no_autosave)

        settings = self.env["res.config.settings"].create({})
        settings.ativ_purchase_no_autosave = True
        settings.set_values()
        self.assertEqual(self.config.get_param(NO_AUTOSAVE_PARAM), "True")
        self.assertTrue(self.env["res.config.settings"].create({}).ativ_purchase_no_autosave)


@tagged("post_install", "-at_install")
class TestPurchaseNoAutosaveSessionInfo(HttpCase):

    def _session_value(self):
        self.authenticate("admin", "admin")
        info = self.make_jsonrpc_request("/web/session/get_session_info")
        self.assertIn("ativ_purchase_no_autosave", info)
        return info["ativ_purchase_no_autosave"]

    def test_session_info_default_on(self):
        self.env["ir.config_parameter"].sudo().search([("key", "=", NO_AUTOSAVE_PARAM)]).unlink()
        self.assertIs(self._session_value(), True)

    def test_session_info_off(self):
        self.env["ir.config_parameter"].sudo().set_param(NO_AUTOSAVE_PARAM, "False")
        self.assertIs(self._session_value(), False)
