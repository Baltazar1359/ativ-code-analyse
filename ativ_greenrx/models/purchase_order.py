from odoo import _, api, fields, models
from odoo.addons.ativ_greenrx.tools import facturx_builder
from odoo.exceptions import UserError

# Fields of the issuer contact the ZUGFeRD seller party cannot be built without.
# Order matters only for the readability of the error message.
ISSUER_REQUIRED_FIELDS = (
    "name",
    "street",
    "city",
    "zip",
    "country_id",
    "vat",
    "company_registry",
    "phone",
    "email",
)

# Order fields whose value ends up inside the factur-x.xml payload: changing one
# of them invalidates an already generated e-invoice. The Studio fields are only
# present on the customer's database, hence the `in self._fields` filter below.
# ``name`` is in there because it is the document number the payload carries
# (`ExchangedDocument/ram:ID`, and the payment reference it falls back to), and
# ``currency_id`` because the payload declares its currency unconditionally
# (`facturx_builder.CURRENCY_CODE`): a sent order moved to another currency has
# to be refused here, at the moment of the write, rather than only surfacing as
# a blocked render later on.
# ``company_id`` is in there for that same currency, not for a value of its own:
# core's ``currency_id`` is a *stored computed* field
# (`@api.depends('partner_id', 'company_id')` →
# `partner.property_purchase_currency_id or company.currency_id`), and the guard
# below can only look at the fields the caller named. A write touching
# ``company_id`` alone therefore moves the currency of a frozen Gutschrift in
# the recompute that follows the write, with ``currency_id`` never appearing in
# ``vals`` for the guard to catch -- silently, until the next render refuses to
# serve the document.
WATCHED_CORE_FIELDS = frozenset({
    "name",
    "partner_id",
    "date_order",
    "currency_id",
    "company_id",
})
WATCHED_STUDIO_FIELDS = (
    "x_studio_vorgang",
    "x_studio_ticket_1",
    "x_studio_complaint_number",
    "x_gutschrift_verwendungszweck",
)

# The freeze flag and the stored hybrid PDF are state owned by this feature:
# `readonly=True` only stops the web client, so the write() guard below is what
# stops an RPC call from unfreezing a sent e-invoice or swapping its PDF. The
# feature's own writes opt out with the `ativ_erechnung_internal_write` context key.
ATIV_ERECHNUNG_PROTECTED_FIELDS = frozenset({
    "ativ_erechnung_sent",
    "ativ_erechnung_pdf_file",
})


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    ativ_erechnung_pdf_file = fields.Binary(
        string="E-Rechnung PDF File",
        attachment=True,
        copy=False,
    )
    ativ_erechnung_pdf_id = fields.Many2one(
        comodel_name="ir.attachment",
        string="E-Rechnung PDF Attachment",
        compute="_compute_ativ_erechnung_pdf_id",
        compute_sudo=True,
    )
    ativ_erechnung_sent = fields.Boolean(
        string="E-Rechnung Sent",
        copy=False,
        readonly=True,
        help="Set once the hybrid ZUGFeRD PDF has been sent out; its data is then frozen.",
    )

    @api.depends("ativ_erechnung_pdf_file")
    def _compute_ativ_erechnung_pdf_id(self):
        """Expose the attachment backing ``ativ_erechnung_pdf_file``.

        Same body as ``account.move._compute_linked_attachment_id``: core has no
        shared mixin for this (``account.move``, ``l10n.in.ewaybill`` and
        ``myinvois.document`` each carry a private copy), and the helper reads
        ``self._name`` / ``self.ids``, so it cannot be borrowed from another
        model. A plain ``Many2one('ir.attachment')`` is avoided because it would
        make every attachment selectable for the user.
        """
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", self._name),
            ("res_id", "in", self.ids),
            ("res_field", "=", "ativ_erechnung_pdf_file"),
        ])
        attachment_per_order = {att.res_id: att for att in attachments}
        for order in self:
            order.ativ_erechnung_pdf_id = attachment_per_order.get(order._origin.id, False)

    def _check_ativ_erechnung_data(self):
        """Raise a field-naming ``UserError`` when an order cannot yield valid XML.

        A final Gutschrift must never exist without a valid e-invoice, so this
        runs before the state change rather than after it. A pass-through when
        the feature is switched off.
        """
        settings = self.env["res.config.settings"]
        if not settings._ativ_erechnung_is_enabled():
            return
        issuer = settings._ativ_erechnung_get_issuer_partner()
        # Translated once per call, not once per order.
        field_labels = {
            "name": _("name"),
            "street": _("street"),
            "city": _("city"),
            "zip": _("zip code"),
            "country_id": _("country"),
            "vat": _("VAT id"),
            "company_registry": _("company registry number"),
            "phone": _("phone number"),
            "email": _("email address"),
        }
        for order in self:
            errors = []
            if not issuer:
                errors.append(_("no e-invoice issuer contact configured (Settings → ATIV)"))
            else:
                for field_name in ISSUER_REQUIRED_FIELDS:
                    if not issuer[field_name]:
                        errors.append(_(
                            "%(partner)s: missing %(field)s",
                            partner=issuer.display_name,
                            field=field_labels[field_name],
                        ))
                bank = issuer.bank_ids.filtered("active")[:1]
                if not bank:
                    errors.append(_(
                        "%(partner)s: missing bank account",
                        partner=issuer.display_name,
                    ))
                else:
                    if not bank.acc_number:
                        errors.append(_(
                            "%(partner)s: missing bank account number (IBAN)",
                            partner=issuer.display_name,
                        ))
                    if not bank.bank_id.bic:
                        errors.append(_(
                            "%(partner)s: missing bank identifier code (BIC)",
                            partner=issuer.display_name,
                        ))
            if not order.partner_id.country_id:
                errors.append(_(
                    "%(partner)s: missing country",
                    partner=order.partner_id.display_name,
                ))
            # The payload states its currency unconditionally — ATIV invoices in
            # euro only, hence the builder's hardcoded ``CURRENCY_CODE``, which
            # is read from there rather than repeated so the guard and the
            # document can never disagree. An order agreed in another currency
            # would therefore leave as a document declaring euro amounts that
            # are not euro, which is why it is refused here instead of being
            # converted: picking a rate on the customer's behalf is a business
            # decision this feature must not take by itself.
            if order.currency_id.name != facturx_builder.CURRENCY_CODE:
                errors.append(_(
                    "%(name)s: currency is %(currency)s, but the e-Rechnung "
                    "format requires %(required)s",
                    name=order.display_name,
                    currency=order.currency_id.name,
                    required=facturx_builder.CURRENCY_CODE,
                ))
            if errors:
                raise UserError(_(
                    "Cannot confirm %(name)s:\n%(errors)s",
                    name=order.display_name,
                    errors="\n".join(errors),
                ))

    def _ativ_erechnung_generate_if_newly_final(self):
        """Render the hybrid PDF of the orders of ``self`` that just became final.

        Shared by both routes to ``state == 'purchase'``: a direct confirmation
        and, under double validation, the later approval. The caller filters
        ``self`` down to the orders worth looking at; this method only skips the
        ones that already carry a stored PDF.
        """
        newly_final = self.filtered(
            lambda o: o.state == "purchase" and not o.ativ_erechnung_pdf_file
        )
        for order in newly_final:
            self.env["ir.actions.report"]._render_qweb_pdf(
                "purchase.report_purchaseorder", [order.id]
            )
            # The report hook stores the PDF through a sudo() environment, so the
            # value is re-read from the database rather than from a stale cache.
            order.invalidate_recordset(["ativ_erechnung_pdf_file"])
            if order.ativ_erechnung_pdf_file:
                order.message_post(body=_("E-Rechnung (ZUGFeRD) generated."))

    def button_confirm(self):
        """Validate the e-invoice data first, then render the hybrid PDF."""
        orders_to_check = self.filtered(lambda o: o.state in ("draft", "sent"))
        # Before super(): a validation failure must leave the order unconfirmed.
        orders_to_check._check_ativ_erechnung_data()
        res = super().button_confirm()
        orders_to_check._ativ_erechnung_generate_if_newly_final()
        return res

    def button_approve(self, force=False):
        """Validate the e-invoice data first, then render the hybrid PDF.

        Under double validation ``button_confirm`` only moves the order to
        ``to approve``; ``purchase`` is reached here. The data is re-validated
        rather than trusted from ``button_confirm``: an order can sit in
        ``to approve`` while its partner or the issuer contact changes, and
        nothing re-checks it in between, so this is the last chance before the
        order becomes final.
        """
        orders_to_check = self.filtered(lambda o: o.state == "to approve")
        # Before super(): a validation failure must leave the order unapproved.
        orders_to_check._check_ativ_erechnung_data()
        res = super().button_approve(force=force)
        self.filtered(lambda o: o.state == "purchase")._ativ_erechnung_generate_if_newly_final()
        return res

    def _ativ_erechnung_check_not_sent(self):
        """Refuse the change when one of the orders of ``self`` already sent its e-invoice.

        A sent e-Rechnung is frozen, and both the header data and the line
        content are part of it — hence the single home for the rule here, called
        before ``super()`` by this model's ``write`` and by every
        ``purchase.order.line`` override, so the whole operation aborts and
        nothing persists.
        """
        already_sent = self.filtered("ativ_erechnung_sent")
        if already_sent:
            raise UserError(_(
                "%(names)s already sent as an e-Rechnung; its data can no longer be changed.",
                names=", ".join(already_sent.mapped("display_name")),
            ))

    def write(self, vals):
        """Regenerate the e-invoice on a data change, or refuse the change once sent."""
        if ATIV_ERECHNUNG_PROTECTED_FIELDS & vals.keys() and not self.env.context.get(
            "ativ_erechnung_internal_write"
        ):
            raise UserError(_(
                "ativ_erechnung_sent and ativ_erechnung_pdf_file are managed by the "
                "e-Rechnung feature and cannot be set directly."
            ))
        watched = WATCHED_CORE_FIELDS | {
            name for name in WATCHED_STUDIO_FIELDS if name in self._fields
        }
        touched = watched & vals.keys()
        if not touched:
            return super().write(vals)
        # Read from self, not from the regenerable subset: an order that was sent
        # stays frozen even if its attachment was cleared or deleted afterwards.
        # Aborts the whole write: nothing is changed.
        self._ativ_erechnung_check_not_sent()
        res = super().write(vals)
        # Filtered after super(): none of the watched fields is the stored PDF, so
        # the attachment reads the same either way, while the sent flag may have
        # been set by this very write() — and a sent e-invoice is never rebuilt.
        self._ativ_erechnung_filter_regenerable()._ativ_erechnung_on_relevant_change()
        return res

    def _ativ_erechnung_filter_regenerable(self):
        """Return the orders of ``self`` whose stored e-invoice may be rebuilt.

        The precondition of ``_ativ_erechnung_on_relevant_change`` — there has
        to be an e-invoice to rebuild, and a sent one is frozen — and its single
        home: the invariant belongs to the order, so ``purchase.order.line``
        reaches the rule through here instead of restating it.
        """
        return self.filtered(
            lambda o: o.ativ_erechnung_pdf_file and not o.ativ_erechnung_sent
        )

    def _ativ_erechnung_on_relevant_change(self):
        """Rebuild the e-invoice of orders that carry one and were not sent yet."""
        for order in self:
            # Cleared so the report hook regenerates instead of reusing the stored
            # PDF; the context key gets this past the protected-fields guard.
            order.with_context(ativ_erechnung_internal_write=True).write({
                "ativ_erechnung_pdf_file": False,
            })
            self.env["ir.actions.report"]._render_qweb_pdf(
                "purchase.report_purchaseorder", [order.id]
            )
            # The report hook stores the PDF through a sudo() environment, so the
            # value is re-read from the database rather than from a stale cache.
            order.invalidate_recordset(["ativ_erechnung_pdf_file"])
            # No replacement produced (the feature was switched off meanwhile, or
            # generation yielded nothing): the PDF is gone, but claiming it was
            # regenerated would be a lie. Staying silent is correct here — the
            # confirmation chokepoint is ``_check_ativ_erechnung_data``, not this.
            if order.ativ_erechnung_pdf_file:
                order.message_post(
                    body=_("E-Rechnung regenerated: the underlying data changed.")
                )

    def _message_post_after_hook(self, message, msg_values):
        """Mark the e-invoice as sent once its PDF leaves the system as an attachment.

        Matched on ``ir.attachment.checksum`` (core's auto-computed sha1 of the
        content) rather than on the attachment id: the mail composer attaches a
        brand-new ``ir.attachment`` copy of the report instead of the one stored
        in ``ativ_erechnung_pdf_file``, so the two id sets never intersect.

        The two exclusions narrow this to a deliberate send: a ``notification``
        is the chatter's own bookkeeping, and ``mail.mt_note`` is "Log a note",
        which the web client posts as ``message_type == 'comment'`` just like
        "Send message" — the subtype is the only thing telling the two apart, so
        re-attaching the PDF internally no longer freezes the order. Narrowing
        false positives is all it is: proving an email really went out would
        mean inspecting ``mail.mail`` / ``mail.notification``, which is out of
        this feature's scope. What the freeze rests on is the checksum match (it
        is this very e-Rechnung) plus "not a pure internal note" (it was meant
        to leave the system).
        """
        res = super()._message_post_after_hook(message, msg_values)
        checksum = self.ativ_erechnung_pdf_id.checksum
        # Resolved defensively although ``mail`` is a dependency of this module's
        # dependencies: an unresolvable subtype must not break posting a message.
        note_subtype = self.env.ref("mail.mt_note", raise_if_not_found=False)
        if (
            message.message_type != "notification"
            and message.subtype_id != note_subtype
            and self.ativ_erechnung_pdf_file
            and checksum
            and checksum in message.attachment_ids.mapped("checksum")
        ):
            self.with_context(ativ_erechnung_internal_write=True).write({
                "ativ_erechnung_sent": True,
            })
        return res
