{
    "name": "ATIV GreenRX",
    "version": "19.0.2.0.0",
    "category": "Technical",
    "summary": "GreenRX customisations: ZUGFeRD e-invoice for Gutschriften, explicit save on purchase orders",
    "author": "ATIV Service GmbH",
    "license": "LGPL-3",
    "depends": ["base", "web", "purchase"],
    "data": [
        "views/res_config_settings_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "ativ_greenrx/static/src/purchase_order_no_autosave.js",
        ],
        "web.assets_unit_tests": [
            "ativ_greenrx/static/tests/**/*",
        ],
    },
    "installable": True,
    "application": False,
}
