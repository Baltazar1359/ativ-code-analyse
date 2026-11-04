# odoosh-ativ

Odoo 19 Enterprise addons for the GreenRX Odoo.sh project (ATIV Service GmbH).

## `ativ_greenrx` — ATIV GreenRX

One module with all GreenRX customisations:

- **E-Rechnung (ZUGFeRD).** Embeds a ZUGFeRD / Factur-X `factur-x.xml` (EN 16931) into the
  PDF of a Gutschrift (`purchase.order`, report `purchase.report_purchaseorder`), producing a
  PDF/A-3 hybrid. Code: `models/purchase_order*.py`, `models/ir_actions_report.py`,
  `tools/facturx_builder.py`.
- **Explicit save on purchase orders.** The purchase order form never autosaves; leaving a
  dirty record asks Save / Discard. Code: `static/src/purchase_order_no_autosave.js`.

### Settings (Settings > ATIV)

| Setting | Settings field | Config parameter | Default |
|---|---|---|---|
| E-Rechnung (ZUGFeRD) | `ativ_erechnung_enabled` | `ativ_greenrx.erechnung_enabled` | off |
| Issuer (seller party) | `ativ_erechnung_issuer_partner_id` | `ativ_greenrx.erechnung_issuer_partner_id` | none |
| Purchase orders: explicit save only | `ativ_purchase_no_autosave` | `ativ_greenrx.purchase_no_autosave` | **on** (also when the parameter is absent) |

The explicit-save toggle is stored as the string `"True"`/`"False"` (not via
`config_parameter=`, which deletes the parameter on False). It reaches the web client as
`session.ativ_purchase_no_autosave`; changes apply after a page reload.

### Tests

- Python (`ativ_greenrx/tests/`, tagged `post_install`): `test_facturx_builder.py`,
  `test_purchase_order_erechnung.py`, `test_settings.py`. Reduced Factur-X XSDs in
  `tests/data/` (see its README).
- JS (Hoot, bundle `web.assets_unit_tests`): `ativ_greenrx/static/tests/`.

```bash
odoo-bin -d <db> -i ativ_greenrx --test-enable --test-tags /ativ_greenrx --stop-after-init
```

The final ZUGFeRD conformance check (Mustang-CLI on a real generated PDF) is a manual
release step.

## Migration from the former modules

`ativ_greenrx` replaces three earlier modules: `ativ_base`, `ativ_erechnung` and
`ativ_purchase_no_autosave`. No data migration is provided on purpose (nothing was produced
with them yet). On a database that has them installed, uninstall `ativ_erechnung`,
`ativ_purchase_no_autosave` (if installed) and `ativ_base` first, then install
`ativ_greenrx` and re-enter the settings.
