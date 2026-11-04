from odoo import api, models

# Line fields whose value ends up inside the factur-x.xml payload: changing one
# of them invalidates the already generated e-invoice of the parent order just
# as a changed header field does. The Studio field is only present on the
# customer's database, hence the `in self._fields` filter below.
WATCHED_LINE_CORE_FIELDS = frozenset({
    "product_id",
    "product_qty",
    "price_unit",
    "tax_ids",
    "discount",
    "product_uom_id",
    # Not a payload field itself, but moving a line between orders changes the
    # content of both the order it leaves and the one it joins.
    "order_id",
})
WATCHED_LINE_STUDIO_FIELDS = ("x_studio_ticket",)


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    def _ativ_erechnung_regenerate(self, orders):
        """Rebuild the e-invoice of the orders of ``orders`` that carry one.

        Which of them those are is the order's own rule
        (``_ativ_erechnung_filter_regenerable``), where the invariant lives;
        this side of the funnel only says *when* to apply it.
        """
        orders._ativ_erechnung_filter_regenerable()._ativ_erechnung_on_relevant_change()

    @api.model_create_multi
    def create(self, vals_list):
        """Regenerate the e-invoice of the parent order, or refuse the new line."""
        # Before super(): adding a line to a sent order must not persist.
        # Deduplicated in order of appearance so the error message is stable.
        order_ids = list(dict.fromkeys(
            vals["order_id"] for vals in vals_list if vals.get("order_id")
        ))
        # A line created as a nested command of `purchase.order.write` needs no
        # special case: the ORM resolves the parent into the nested vals before
        # calling this create(), so `order_id` is already there to be read.
        self.env["purchase.order"].browse(order_ids)._ativ_erechnung_check_not_sent()
        records = super().create(vals_list)
        self._ativ_erechnung_regenerate(records.order_id)
        return records

    def write(self, vals):
        """Regenerate the e-invoice on a line data change, or refuse the change."""
        watched = WATCHED_LINE_CORE_FIELDS | {
            name for name in WATCHED_LINE_STUDIO_FIELDS if name in self._fields
        }
        touched = watched & vals.keys()
        if not touched:
            return super().write(vals)
        # Read before super(): afterwards the lines no longer point at the order
        # they are leaving, and its e-invoice is just as stale as the new one's.
        orders = self.order_id
        if "order_id" in touched and vals.get("order_id"):
            orders |= self.env["purchase.order"].browse(vals["order_id"])
        orders._ativ_erechnung_check_not_sent()
        res = super().write(vals)
        self._ativ_erechnung_regenerate(orders)
        return res

    def unlink(self):
        """Regenerate the e-invoice of the parent order, or refuse the deletion.

        A removed line changes the e-invoice content exactly like an edited one.
        """
        # Read before super(): the lines are gone afterwards, and with them the
        # only link to the orders whose e-invoice has to be rebuilt.
        orders = self.order_id
        orders._ativ_erechnung_check_not_sent()
        res = super().unlink()
        self._ativ_erechnung_regenerate(orders)
        return res
