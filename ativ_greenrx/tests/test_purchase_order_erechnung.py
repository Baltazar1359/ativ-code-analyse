"""Integration tests of the e-Rechnung feature on ``purchase.order``.

Where ``test_facturx_builder`` checks the XML mapping as a pure function, these
tests drive the feature the way a user does — ``button_confirm``, a field
change, an edit to the issuer contact in the Contacts app, a mail with the PDF
attached — and let every layer run for real: the validation in
``purchase.order``, the report hook in ``ir_actions_report``, the PDF/A-3
conversion in ``odoo.tools.pdf`` and the ``factur-x.xml`` embedding.

The feature keeps a stored e-Rechnung in step with its data in two places, and
both are tested here because each catches what the other cannot: the
``write()`` hooks of ``purchase.order``/``purchase.order.line`` fire on a
change to the order itself, and the render chokepoint rebuilds and compares the
payload on every send, download and print, which is the only thing that can see
a change to master data on another model
(``test_regenerate_on_issuer_iban_change_before_sent_then_block_after_sent``).

Making the report actually render in a test run takes two deliberate pieces of
scaffolding, both of them precedented in Odoo's own suite
(``account_edi_ubl_cii/tests/test_ubl_import_bis3_invoice_be_auto_generate_pdf.py``):

* **``force_report_rendering``.** ``ir.actions.report._pre_render_qweb_pdf``
  short-circuits to ``_render_qweb_html`` whenever a test is running, unless the
  context carries this key. Without it the PDF branch — and therefore this
  feature's whole hook — is never reached, and every assertion below would pass
  vacuously on an HTML string. It is set on the class environment rather than
  per call because the feature renders from inside the ORM
  (``_ativ_erechnung_generate_if_newly_final`` and
  ``_ativ_erechnung_on_relevant_change`` both call
  ``self.env['ir.actions.report']._render_qweb_pdf``), so the key has to be on
  the environment the records themselves carry, not on a recordset at the call
  site.

* **A fake ``_run_wkhtmltopdf``.** ``wkhtmltopdf`` is a binary, not a Python
  dependency, and it is absent from this runtime. Patched here is only the one
  method that shells out to it; everything downstream of the subprocess —
  the QWeb rendering of ``purchase.report_purchaseorder``, the per-record stream
  collection, this module's ``_render_qweb_pdf_prepare_streams`` override, the
  conversion and the ORM writes — runs exactly as in production. The core's
  pre-flight ``get_wkhtmltopdf_state`` check is patched alongside it: it raises
  "Unable to find Wkhtmltopdf on this system" *before* the subprocess call, so
  leaving it in place would make the fake unreachable.

The fake returns a real, parseable PDF built with ``reportlab`` (a declared Odoo
dependency, already used inside ``odoo.tools.pdf``) and then given an
``/Outlines`` entry in its catalog — see ``_build_minimal_pdf``. That one entry
is what lets the happy path genuinely exercise PDF/A-3 conversion plus hybrid
embedding instead of dying in ``convert_to_pdfa``.

**A purchase order's PDF already carries an XML before we touch it.** Core's
``purchase`` addon embeds a ``<order>_ubl_bis3.xml`` UBL Order document into
every ``report_purchaseorder`` / ``report_purchasequotation`` render, in every
state, from its own ``_render_qweb_pdf_prepare_streams`` below this module in
the MRO. The finished hybrid therefore holds **two** embedded XMLs, and the
assertions below are written against the ``factur-x.xml`` by name rather than
against the attachment list as a whole. Whether a strict ZUGFeRD validator
accepts that second, XMP-undeclared file is a conformance question for
Mustang-CLI on the Odoo.sh build, not something this suite can settle.

**What this environment still cannot prove** (named in the plan's human-checked
gaps, and unchanged by these tests): that the finished hybrid PDF is *valid*
PDF/A-3 and valid Factur-X to an external validator. The bytes here come from
``reportlab`` and not from ``wkhtmltopdf``, and the fixture carries no font
resources at all (see ``_build_minimal_pdf``), so ``convert_to_pdfa``'s
glyph-width rewriting branch — the one that reads ``/DescendantFonts`` off
every font of a real wkhtmltopdf document and needs ``fontTools`` for it — runs
over an empty set rather than over real glyphs. Conformance stays a human's
check with Mustang-CLI on the Odoo.sh dev build.
"""

import base64
import io
from datetime import datetime
from unittest.mock import patch

from lxml import etree
from reportlab.pdfgen.canvas import Canvas

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger
from odoo.tools.pdf import (
    DictionaryObject,
    NameObject,
    OdooPdfFileReader,
    OdooPdfFileWriter,
)

from odoo.addons.ativ_greenrx.models.res_config_settings import (
    ENABLED_PARAM,
    ISSUER_PARTNER_PARAM,
)

# The report the feature hooks, and the name the Factur-X specification fixes
# for the embedded payload. Spelled out rather than imported from the module
# under test: these are the external contract — the report a user prints and
# the filename a reader looks for — and reading them back out of the code would
# follow a typo there instead of catching it.
CREDIT_NOTE_REPORT = "purchase.report_purchaseorder"
FACTURX_FILENAME = "factur-x.xml"

# Only the three namespaces these tests read out of the payload.
NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}

# The issuer's bank account as the fixture configures it, and the two values the
# master-data test moves it to. Named constants because that test asserts on
# them by value: what it is about is these exact strings travelling from the
# Contacts app into the payload's ``ram:IBANID``, and a literal typed twice
# could agree with itself while agreeing with nothing the code did.
ISSUER_IBAN = "DE89370400440532013000"
ISSUER_IBAN_CHANGED = "DE02500105170648489890"
ISSUER_IBAN_CHANGED_AGAIN = "DE75512108001245126199"

# The calendar date of the fixture's ``date_order`` (2026-01-15 10:30 UTC) in
# UTC, in the CCYYMMDD format qualifier 102 fixes for the payload's
# ``udt:DateTimeString``.
ISSUE_DATE_UTC = "20260115"

# A stored e-Rechnung that has gone bad — a truncated upload, a damaged
# filestore. Valid base64 of content no PDF parser can read, because that is the
# shape such a file really has in the database: ``ir.attachment`` decodes the
# value on write (``_inverse_datas``), so invalid base64 never reaches storage
# through the ORM at all.
CORRUPTED_PDF_CONTENT = b"this used to be a hybrid ZUGFeRD PDF"

# UTC+14, the furthest timezone east there is: half past ten UTC is half past
# midnight of the *next* day in it. That is the whole reason it is this one —
# the timezone test below needs a reader whose calendar date differs from the
# date the stored e-Rechnung was stamped with, and no mid-morning UTC order
# gives one in a nearer zone.
TZ_NEXT_DAY = "Pacific/Kiritimati"


@tagged("post_install", "-at_install")
class TestPurchaseOrderErechnung(TransactionCase):
    """The feature end to end: validate, generate, regenerate, freeze."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # See the module docstring: without this key the core renders HTML
        # instead of a PDF under a test run and the hook is never reached. Set
        # on the class environment so every record created below carries it,
        # and so the `self.env['ir.actions.report']` the feature reaches for
        # from inside `button_confirm` and `write` inherits it too.
        cls.env = cls.env(context=dict(cls.env.context, force_report_rendering=True))

        germany = cls.env.ref("base.de")

        # Every field `_check_ativ_erechnung_data` requires of the issuer, so
        # the fixture passes validation and the tests that want a failure can
        # remove exactly one field. Same shape as `test_facturx_builder`.
        cls.issuer = cls.env["res.partner"].create({
            "name": "ATIV Service GmbH",
            "street": "Hauptstrasse 1",
            "city": "Berlin",
            "zip": "10115",
            "country_id": germany.id,
            "vat": "DE123456789",
            "company_registry": "HRB 123456 B",
            "phone": "+49 30 1234560",
            "email": "e-rechnung@ativ-service.example",
        })
        bank = cls.env["res.bank"].create({
            "name": "Commerzbank Berlin",
            "bic": "COBADEFFXXX",
        })
        cls.env["res.partner.bank"].create({
            "acc_number": ISSUER_IBAN,
            "partner_id": cls.issuer.id,
            "bank_id": bank.id,
        })

        # The only currency this feature issues a Gutschrift in, pinned here
        # because `_create_order` sets it explicitly — see the reason there.
        cls.currency_eur = cls.env.ref("base.EUR")

        # Two complete buyers: the second one is what the regeneration test
        # changes the order over to, and its name is how that test tells the
        # rebuilt payload from the original one.
        #
        # ``property_purchase_currency_id`` is what makes them euro vendors, and
        # it is not decoration: core's ``purchase.order.currency_id`` is a
        # *stored computed* field (``_compute_currency_id`` →
        # ``partner.property_purchase_currency_id or company.currency_id``), so
        # writing ``partner_id`` on an existing order recomputes it. Without the
        # property the recompute falls back to the test database's company
        # currency -- USD on a bare Odoo install -- which would silently flip a
        # euro order to USD behind a test that only meant to change the buyer,
        # and `_check_ativ_erechnung_data` would then rightly refuse to rebuild
        # its e-Rechnung. Pinning it here keeps "the buyer changed" the only
        # thing those tests change, and makes the EUR-only tests below
        # independent of whatever currency the install happens to carry.
        cls.buyer = cls.env["res.partner"].create({
            "name": "Apotheke GreenRX",
            "country_id": germany.id,
            "property_purchase_currency_id": cls.currency_eur.id,
        })
        cls.buyer_other = cls.env["res.partner"].create({
            "name": "Apotheke BlueRX",
            "country_id": germany.id,
            "property_purchase_currency_id": cls.currency_eur.id,
        })
        # Only its country is missing, which is the one buyer field the
        # validation requires: used by the switched-off test to prove nothing
        # is validated there.
        cls.buyer_incomplete = cls.env["res.partner"].create({
            "name": "Apotheke Unvollstaendig",
            "property_purchase_currency_id": cls.currency_eur.id,
        })

        cls.tax = cls.env["account.tax"].create({
            "name": "Test USt 19% (ATIV)",
            "amount": 19.0,
            "amount_type": "percent",
            "type_tax_use": "purchase",
        })
        cls.product = cls.env["product.product"].create({
            "name": "Test-Arzneimittel",
            "default_code": "PZN-00000001",
            "purchase_ok": True,
        })

        cls.order = cls._create_order(cls.buyer)

        # Built once: the bytes stand in for whatever wkhtmltopdf would have
        # produced, and they are the same for every render, so any difference
        # between two hybrid PDFs below comes from the embedded XML alone.
        cls.minimal_pdf = cls._build_minimal_pdf()

    @classmethod
    def _create_order(cls, partner, currency=None):
        """Return a new confirmable one-line order for ``partner``.

        A fixed ``date_order`` keeps the issue date — and therefore the payload
        and the bytes built from it — identical on every run.

        The currency is set explicitly rather than left to core's compute, which
        falls back to the company's: this feature issues euro Gutschriften only
        and ``_check_ativ_erechnung_data`` refuses anything else, so an order
        left on whatever currency the test database's company happens to carry
        would make every test below depend on that. ``currency`` is what the
        non-euro test hands in.
        """
        return cls.env["purchase.order"].create({
            "partner_id": partner.id,
            "currency_id": (currency or cls.currency_eur).id,
            "date_order": datetime(2026, 1, 15, 10, 30, 0),
            "order_line": [(0, 0, {
                "product_id": cls.product.id,
                "name": "Test-Arzneimittel",
                "product_qty": 3.0,
                "price_unit": 49.5,
                "tax_ids": [(6, 0, cls.tax.ids)],
            })],
        })

    @staticmethod
    def _build_minimal_pdf():
        """Return the bytes of a one-page PDF that survives ``convert_to_pdfa``.

        ``reportlab`` alone is not enough, for two reasons, both rooted in
        structures ``OdooPdfFileWriter.convert_to_pdfa`` takes for granted
        because every wkhtmltopdf document happens to have them:

        * It writes a catalog with no ``/Outlines`` entry, and the conversion
          reads ``self._root_object['/Outlines']`` unguarded. The second pass
          below adds that entry through ``get_outline_root()``, the writer's
          own API for it.
        * Its fonts are simple ``/Type1`` (Helvetica) or ``/TrueType`` subsets,
          never the composite ``/Type0`` fonts wkhtmltopdf emits, and the
          conversion's glyph-width rewrite reads ``/DescendantFonts`` off every
          font on every page unguarded -- whenever ``fontTools`` is importable,
          which on the Odoo.sh build it is (``requirements.txt``). The second
          pass therefore also empties each page's ``/Font`` resources: the
          text operators in the content stream then reference a font the page
          no longer declares, which no test here renders or reads, and the
          rewrite loop has nothing to do. ``reportlab`` cannot produce a
          ``/Type0`` font, so a realistic fixture is not an option.

        Pages are walked through ``_root_object['/Pages']``, as the conversion
        itself does: after ``cloneReaderDocumentRoot`` the writer's own page
        list is still empty.
        """
        buffer = io.BytesIO()
        canvas = Canvas(buffer)
        canvas.drawString(72, 720, "ATIV Gutschrift test fixture")
        canvas.save()
        flat_pdf = buffer.getvalue()
        buffer.close()

        reader = OdooPdfFileReader(io.BytesIO(flat_pdf), strict=False)
        writer = OdooPdfFileWriter()
        writer.cloneReaderDocumentRoot(reader)
        # Creates the catalog's ``/Outlines`` entry as a side effect; the empty
        # outline tree it returns is exactly what a document without bookmarks
        # carries.
        writer.get_outline_root()
        for page_ref in writer._root_object["/Pages"]["/Kids"]:
            page = page_ref.getObject()
            resources = DictionaryObject({
                NameObject(key): value
                for key, value in page["/Resources"].items()
                if key != "/Font"
            })
            resources[NameObject("/Font")] = DictionaryObject()
            page[NameObject("/Resources")] = resources
        out = io.BytesIO()
        writer.write(out)
        fixture = out.getvalue()
        out.close()
        return fixture

    def setUp(self):
        super().setUp()
        report_model = self.env.registry["ir.actions.report"]
        fixture = self.minimal_pdf

        def _run_wkhtmltopdf(*args, **kwargs):
            """Stand in for the absent binary, returning a real PDF."""
            return fixture

        # Patched in setUp rather than per test, and started/stopped through
        # addCleanup, so every test below renders transparently.
        patcher = patch.object(report_model, "_run_wkhtmltopdf", _run_wkhtmltopdf)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The core refuses to render at all when the binary is missing, and it
        # checks that before calling the method patched above.
        state_patcher = patch.object(
            report_model, "get_wkhtmltopdf_state", lambda self: "ok"
        )
        state_patcher.start()
        self.addCleanup(state_patcher.stop)

        self._configure_erechnung(enabled=True, issuer=self.issuer)

    def _configure_erechnung(self, enabled, issuer):
        """Switch the feature on or off, and point it at ``issuer``.

        Writes the two ``ir.config_parameter`` rows the feature actually reads
        (``_ativ_erechnung_is_enabled`` and
        ``_ativ_erechnung_get_issuer_partner``) rather than going through
        ``res.config.settings.execute()``: the settings record is transient and
        shared with every other installed module's settings group, so saving it
        would run all of their ``set_values`` as a side effect of switching one
        boolean. The parameter names come from the module under test, so a
        rename follows through to here.
        """
        config = self.env["ir.config_parameter"].sudo()
        # str(bool) is what the Boolean settings field stores, and what
        # `str2bool` reads back.
        config.set_param(ENABLED_PARAM, str(bool(enabled)))
        config.set_param(ISSUER_PARTNER_PARAM, str(issuer.id) if issuer else "")

    def _stored_attachments(self, order):
        """Return the ``ir.attachment`` records backing ``order``'s stored PDF.

        Searched rather than read off ``ativ_erechnung_pdf_id``: the point of
        the idempotence assertions is the number of rows in the database, which
        a Many2one pointing at one of them cannot report.
        """
        return self.env["ir.attachment"].search([
            ("res_model", "=", "purchase.order"),
            ("res_id", "=", order.id),
            ("res_field", "=", "ativ_erechnung_pdf_file"),
        ])

    @staticmethod
    def _pdf_attachments(pdf_bytes):
        """Return the embedded files of ``pdf_bytes`` as ``(name, content)``.

        A list and not a dict: a second ``factur-x.xml`` embedded by a
        double-converted PDF is precisely what the idempotence tests look for,
        and a dict would hide it.
        """
        reader = OdooPdfFileReader(io.BytesIO(pdf_bytes), strict=False)
        return list(reader.getAttachments())

    def _facturx_payloads(self, pdf_bytes):
        """Return the contents of every ``factur-x.xml`` embedded in ``pdf_bytes``.

        Filtered by name rather than taking the whole attachment list, because
        this feature is not the only thing embedding a file in a purchase
        order's PDF: core's own ``purchase`` addon
        (``addons/purchase/models/ir_actions_report.py``) attaches a
        ``<order>_ubl_bis3.xml`` UBL Order document to every
        ``report_purchaseorder`` and ``report_purchasequotation`` render, in
        every state. It sits below this module in the MRO, so our override
        converts a PDF that already carries it and ``cloneReaderDocumentRoot``
        preserves it — the finished hybrid legitimately holds two XMLs, and
        only the ``factur-x.xml`` is ours to assert on.
        """
        return [
            content
            for name, content in self._pdf_attachments(pdf_bytes)
            if name == FACTURX_FILENAME
        ]

    def _assert_carries_facturx_for(self, pdf_bytes, order):
        """Assert ``pdf_bytes`` embeds exactly one ``factur-x.xml``, ``order``'s."""
        payloads = self._facturx_payloads(pdf_bytes)
        self.assertEqual(
            len(payloads), 1,
            "the hybrid PDF does not carry exactly one %s (embedded files: %s)"
            % (FACTURX_FILENAME, [n for n, _c in self._pdf_attachments(pdf_bytes)]),
        )
        document = etree.fromstring(payloads[0])
        self.assertEqual(
            document.tag, "{%s}CrossIndustryInvoice" % NS["rsm"],
            "the embedded file is not a Cross Industry Invoice",
        )
        self.assertEqual(
            document.findtext("rsm:ExchangedDocument/ram:ID", namespaces=NS),
            order.name,
            "the embedded payload belongs to another order",
        )
        return document

    @staticmethod
    def _buyer_name(document):
        """Return the buyer party's name in ``document``."""
        return document.findtext(
            "rsm:SupplyChainTradeTransaction/ram:ApplicableHeaderTradeAgreement"
            "/ram:BuyerTradeParty/ram:Name",
            namespaces=NS,
        )

    @staticmethod
    def _payment_iban(document):
        """Return the IBAN ``document`` names as the Gutschrift's payment account.

        The one payload value that comes from neither the order nor its lines
        but from the issuer contact's bank account, which is what makes it the
        thing to assert on for a master-data change the order itself never saw.
        """
        return document.findtext(
            "rsm:SupplyChainTradeTransaction/ram:ApplicableHeaderTradeSettlement"
            "/ram:SpecifiedTradeSettlementPaymentMeans"
            "/ram:PayeePartyCreditorFinancialAccount/ram:IBANID",
            namespaces=NS,
        )

    @staticmethod
    def _issue_date(document):
        """Return the issue date ``document`` states, as the CCYYMMDD it holds.

        The one payload value that is a statement about neither the order nor
        the issuer but about *when it was written down*, and the only one
        resolved in the reader's timezone — which is what makes it the thing to
        assert on for a download from somewhere else.
        """
        return document.findtext(
            "rsm:ExchangedDocument/ram:IssueDateTime/udt:DateTimeString",
            namespaces=NS,
        )

    def _render(self, order, env=None):
        """Render ``order``'s Credit Note report and return the PDF bytes.

        ``env`` is for the tests about *who* is rendering — a reader in another
        timezone, a portal user downloading their own Gutschrift. The hook reads
        both off the environment the report recordset carries (the timezone
        through ``fields.Datetime.context_timestamp``, the access rights through
        every ORM read it does), so neither can be handed in as an argument, and
        a recordset in the class environment would quietly test the superuser
        again. Defaults to the class environment, which is what every other test
        here wants.
        """
        report_model = (env or self.env)["ir.actions.report"]
        pdf_content, report_type = report_model._render_qweb_pdf(
            CREDIT_NOTE_REPORT, [order.id]
        )
        # Guards the whole file: if the core ever short-circuits to HTML again,
        # every assertion about embedded XML would silently pass on a string.
        self.assertEqual(
            report_type, "pdf",
            "the report did not render as a PDF, so nothing below was exercised",
        )
        return pdf_content

    def _post_as_sent(self, order):
        """Post ``order``'s stored e-Rechnung the way actually sending it does.

        Two details decide whether this freezes the order, and both are the
        mail composer's:

        * **a fresh attachment** with the same content rather than the stored
          record itself — that is what the composer produces, and what the
          checksum match in ``_message_post_after_hook`` is there to recognise;
        * **``mail.mt_comment``** as the subtype. ``message_post`` defaults to
          ``mail.mt_note``, which is "Log a note" and deliberately must *not*
          freeze anything (see
          ``test_internal_note_with_the_pdf_does_not_freeze_the_order``), so a
          test leaving the default would assert the opposite of what it means
          to.
        """
        return order.message_post(
            body="Gutschrift sent to the vendor",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            attachments=[("Gutschrift.pdf", order.ativ_erechnung_pdf_id.raw)],
        )

    def test_draft_pdf_has_no_facturx(self):
        """A draft order's PDF carries no e-invoice (R1).

        A draft purchase order is a Request for Quotation, not a Gutschrift, so
        the hook has to hand the core's own bytes straight back even with the
        feature switched on — which it is here, which is what makes this test
        about the state filter and not about the switch.

        "No e-invoice" means no ``factur-x.xml``, not an empty attachment list:
        core's ``purchase`` addon embeds its own UBL Order XML into this report
        in every state (see ``_facturx_payloads``), and that file is none of
        this feature's business.
        """
        self.assertEqual(self.order.state, "draft")

        pdf_content = self._render(self.order)

        self.assertEqual(
            self._facturx_payloads(pdf_content), [],
            "a draft order's PDF carries a factur-x.xml",
        )
        self.assertFalse(
            self.order.ativ_erechnung_pdf_file,
            "a draft order's render stored a hybrid PDF",
        )
        self.assertFalse(self._stored_attachments(self.order))

    def test_confirm_blocks_on_missing_issuer_field(self):
        """An incomplete issuer blocks the confirmation and names the field.

        The validation runs before ``super().button_confirm()`` precisely so a
        failure leaves the order unconfirmed — a final Gutschrift without a
        valid e-Rechnung must not come into existence — so the state assertion
        below is as much the point as the error itself.
        """
        self.issuer.vat = False

        with self.assertRaises(UserError) as capture:
            self.order.button_confirm()

        message = str(capture.exception)
        self.assertIn(self.issuer.display_name, message)
        self.assertIn(
            "VAT id", message,
            "the error does not name the field that is missing",
        )
        self.assertIn(self.order.display_name, message)

        self.assertEqual(
            self.order.state, "draft",
            "the order was confirmed although its e-invoice data is incomplete",
        )
        self.assertFalse(self.order.ativ_erechnung_pdf_file)
        self.assertFalse(self._stored_attachments(self.order))

    def test_confirm_blocks_on_missing_issuer_bank(self):
        """A bank account without a BIC blocks the confirmation too.

        The bank lives one relation away from the issuer and is validated by a
        separate branch, so the field loop above does not cover it.
        """
        self.issuer.bank_ids.bank_id.bic = False

        with self.assertRaises(UserError) as capture:
            self.order.button_confirm()

        self.assertIn("BIC", str(capture.exception))
        self.assertEqual(self.order.state, "draft")

    def test_confirm_blocks_on_missing_buyer_country(self):
        """A buyer without a country blocks the confirmation.

        The one buyer field the payload cannot be built without, and the only
        one the validation requires.
        """
        order = self._create_order(self.buyer_incomplete)

        with self.assertRaises(UserError) as capture:
            order.button_confirm()

        message = str(capture.exception)
        self.assertIn(self.buyer_incomplete.display_name, message)
        self.assertIn("country", message)
        self.assertEqual(order.state, "draft")

    def test_confirm_blocks_on_non_eur_currency(self):
        """An order in another currency blocks the confirmation.

        The payload states its currency unconditionally —
        ``facturx_builder.CURRENCY_CODE``, hardcoded because ATIV issues euro
        Gutschriften only — so an order agreed in another currency could only
        leave as a document declaring euro amounts that are not euro: the
        figures would be the order's own, the currency the document claims for
        them somebody else's. Nothing in the order's data says "this is not
        euro" to a reader of the XML, which is why this has to be refused
        before the Gutschrift becomes final rather than mapped and sent.
        """
        order = self._create_order(self.buyer, currency=self.env.ref("base.USD"))
        self.assertNotEqual(
            order.currency_id, self.currency_eur,
            "the fixture order is in euro after all, so nothing below is tested",
        )

        with self.assertRaises(UserError) as capture:
            order.button_confirm()

        message = str(capture.exception)
        self.assertIn(order.display_name, message)
        self.assertIn(
            "USD", message,
            "the error does not name the currency the order is actually in",
        )
        self.assertIn(
            "EUR", message,
            "the error does not name the currency the format requires",
        )

        self.assertEqual(
            order.state, "draft",
            "the order was confirmed although its currency cannot be mapped",
        )
        self.assertFalse(order.ativ_erechnung_pdf_file)
        self.assertFalse(self._stored_attachments(order))

    def test_confirm_generates_hybrid_file_once(self):
        """Confirming stores one hybrid PDF, and later renders reuse it (R4, R6).

        The happy path, and the only test that takes the feature all the way
        through PDF/A-3 conversion and ``factur-x.xml`` embedding: the
        validation passes, the hook converts the rendered stream, stores it and
        posts a note, and every render after that serves the stored bytes back
        byte-for-byte instead of converting again.
        """
        order = self.order
        order.button_confirm()

        self.assertEqual(order.state, "purchase")
        self.assertTrue(
            order.ativ_erechnung_pdf_file,
            "confirming a complete order stored no hybrid PDF",
        )

        attachments = self._stored_attachments(order)
        self.assertEqual(
            len(attachments), 1,
            "the stored hybrid PDF is not backed by exactly one attachment",
        )
        self.assertEqual(order.ativ_erechnung_pdf_id, attachments)

        stored_pdf = attachments.raw
        document = self._assert_carries_facturx_for(stored_pdf, order)
        self.assertEqual(self._buyer_name(document), self.buyer.name)
        # The conversion really ran: `convert_to_pdfa` rewrites the header to
        # 1.7 and is the only thing in this path that does.
        self.assertTrue(
            stored_pdf.startswith(b"%PDF-1.7"),
            "the stored PDF was not converted to PDF/A-3",
        )
        # Core's UBL Order XML survived our conversion rather than being
        # dropped by it. Asserted as "some other XML is still embedded" rather
        # than by its exact name, which is core's to change: what matters is
        # that converting the stream does not throw away what the addon below
        # us put there. The flip side — that a ZUGFeRD reader is happy with a
        # second, undeclared XML next to the factur-x.xml — is a conformance
        # question only Mustang-CLI on the Odoo.sh build can answer.
        other_embedded = [
            name for name, _content in self._pdf_attachments(stored_pdf)
            if name != FACTURX_FILENAME
        ]
        self.assertTrue(
            other_embedded,
            "the conversion dropped the embedded file core's purchase addon added",
        )

        self.assertTrue(
            any("generated" in (body or "") for body in order.message_ids.mapped("body")),
            "no chatter note records the generated e-Rechnung",
        )

        # Generated once, served identically ever after: a second render must
        # neither convert again (which would embed a second factur-x.xml) nor
        # store a second attachment.
        rendered_again = self._render(order)
        self.assertEqual(
            rendered_again, stored_pdf,
            "a later render does not serve the stored hybrid PDF unchanged",
        )
        order.invalidate_recordset(["ativ_erechnung_pdf_file"])
        self.assertEqual(
            self._stored_attachments(order), attachments,
            "a later render stored a second hybrid PDF",
        )
        self.assertEqual(
            len(self._facturx_payloads(rendered_again)), 1,
            "a later render embedded a second factur-x.xml",
        )

    def test_regenerate_before_sent_then_block_after_sent(self):
        """A data change rebuilds the e-invoice, until it has been sent (R2, R5).

        Both halves of the freeze rule in one flow, because the second only
        means anything after the first: while the Gutschrift has not left the
        system a changed buyer must rebuild the payload, and once it has, the
        very same change must be refused outright.
        """
        order = self.order
        order.button_confirm()
        original_pdf = order.ativ_erechnung_pdf_id.raw
        original_checksum = order.ativ_erechnung_pdf_id.checksum
        self.assertFalse(order.ativ_erechnung_sent)

        # --- before sending: the change goes through and rebuilds the payload
        order.partner_id = self.buyer_other

        rebuilt = order.ativ_erechnung_pdf_id
        self.assertTrue(rebuilt, "the regenerated hybrid PDF was not stored")
        self.assertNotEqual(
            rebuilt.checksum, original_checksum,
            "the stored PDF did not change although the buyer did",
        )
        self.assertNotEqual(rebuilt.raw, original_pdf)
        # The decisive assertion: not merely different bytes, but a payload
        # rebuilt from the new data.
        document = self._assert_carries_facturx_for(rebuilt.raw, order)
        self.assertEqual(
            self._buyer_name(document), self.buyer_other.name,
            "the rebuilt payload still names the previous buyer",
        )
        self.assertEqual(
            len(self._stored_attachments(order)), 1,
            "regenerating left the previous attachment behind",
        )
        self.assertTrue(
            any("regenerated" in (body or "") for body in order.message_ids.mapped("body")),
            "no chatter note records the regenerated e-Rechnung",
        )

        # --- sending it: a mail carrying a copy of the stored PDF freezes it.
        self._post_as_sent(order)
        self.assertTrue(
            order.ativ_erechnung_sent,
            "posting the e-Rechnung as an attachment did not freeze it",
        )

        # --- after sending: the same change is refused, and nothing moves
        frozen_checksum = order.ativ_erechnung_pdf_id.checksum
        with self.assertRaises(UserError) as capture:
            order.partner_id = self.buyer
        self.assertIn(order.display_name, str(capture.exception))

        self.assertEqual(
            order.partner_id, self.buyer_other,
            "the buyer changed on an order whose e-Rechnung was already sent",
        )
        self.assertEqual(
            order.ativ_erechnung_pdf_id.checksum, frozen_checksum,
            "the frozen e-Rechnung was rebuilt",
        )

    def test_regenerate_on_issuer_iban_change_before_sent_then_block_after_sent(self):
        """Changed issuer master data is caught at render time (R2, R5).

        The ``write()`` hooks of ``purchase.order`` and ``purchase.order.line``
        watch the fields of those two models, and the IBAN is on neither: it
        lives on the issuer contact's ``res.partner.bank``, which somebody edits
        in the Contacts app with nothing on the order changing at all. No hook
        can fire, so without the check at the render chokepoint the stored PDF
        would go on being served with the old payment details on it forever —
        the one failure that costs money, because a payer reads the IBAN out of
        the document and not out of Odoo.

        The assertion right after the change is what makes this a test of that
        check and not of the write-time one: the stored file is still the old one
        at that point, i.e. nothing fired, and everything below it is the second
        net doing the work.

        Both halves of the freeze rule hold there too, for the reason they hold
        on a write (R5): while the Gutschrift has not left the system the stale
        file is rebuilt, and once it has, the print is refused rather than
        serving a document that disagrees with the one the recipient holds.
        """
        order = self.order
        order.button_confirm()
        original_checksum = order.ativ_erechnung_pdf_id.checksum
        self.assertEqual(
            self._payment_iban(
                self._assert_carries_facturx_for(order.ativ_erechnung_pdf_id.raw, order)
            ),
            ISSUER_IBAN,
            "the generated e-Rechnung does not carry the issuer's bank account",
        )
        self.assertFalse(order.ativ_erechnung_sent)

        # --- the issuer's bank account changes, and nothing on the order does
        self.issuer.bank_ids.acc_number = ISSUER_IBAN_CHANGED

        self.assertEqual(
            order.ativ_erechnung_pdf_id.checksum, original_checksum,
            "the write-time hooks rebuilt the e-Rechnung on a change to the "
            "issuer contact, so the render-time check is not what is exercised "
            "below",
        )

        # --- before sending: the next render rebuilds the stale file
        rendered = self._render(order)
        # The hook stores through a sudo() environment, so both the file and the
        # chatter are re-read from the database rather than from a stale cache.
        order.invalidate_recordset(["ativ_erechnung_pdf_file", "message_ids"])

        self.assertEqual(
            self._payment_iban(self._assert_carries_facturx_for(rendered, order)),
            ISSUER_IBAN_CHANGED,
            "the served PDF still names the issuer's previous bank account",
        )
        rebuilt = self._stored_attachments(order)
        self.assertEqual(
            len(rebuilt), 1,
            "regenerating left the previous attachment behind",
        )
        self.assertNotEqual(
            rebuilt.checksum, original_checksum,
            "the stale file was served to the caller but never replaced in "
            "storage, so the next send would carry it again",
        )
        self.assertEqual(
            rebuilt.raw, rendered,
            "the bytes served and the bytes stored are not the same document",
        )
        self.assertTrue(
            any("regenerated" in (body or "") for body in order.message_ids.mapped("body")),
            "no chatter note records the regenerated e-Rechnung",
        )

        # --- sending it: a mail carrying a copy of the stored PDF freezes it
        self._post_as_sent(order)
        self.assertTrue(
            order.ativ_erechnung_sent,
            "posting the e-Rechnung as an attachment did not freeze it",
        )
        frozen_checksum = order.ativ_erechnung_pdf_id.checksum

        # --- after sending: the same kind of change refuses the render outright
        self.issuer.bank_ids.acc_number = ISSUER_IBAN_CHANGED_AGAIN

        with self.assertRaises(UserError) as capture:
            self._render(order)
        self.assertIn(order.display_name, str(capture.exception))

        order.invalidate_recordset(["ativ_erechnung_pdf_file"])
        self.assertEqual(
            self._stored_attachments(order).checksum, frozen_checksum,
            "the frozen e-Rechnung was rebuilt",
        )
        self.assertEqual(
            self._payment_iban(
                self._assert_carries_facturx_for(order.ativ_erechnung_pdf_id.raw, order)
            ),
            ISSUER_IBAN_CHANGED,
            "the payment details of the e-Rechnung the vendor holds changed",
        )

    def _corrupt_stored_pdf(self, order):
        """Replace ``order``'s stored hybrid PDF with bytes that are not a PDF.

        Written through the feature's own opt-out context key because the
        protected-fields guard refuses every other write to this field: what is
        being set up is a stored file that has gone bad, not a way around that
        guard, and ``ativ_erechnung_pdf_file`` is not a watched field, so this
        write stays clear of the freeze rule as well.
        """
        order.with_context(ativ_erechnung_internal_write=True).write({
            "ativ_erechnung_pdf_file": base64.b64encode(CORRUPTED_PDF_CONTENT),
        })
        # The field is backed by an attachment the write just replaced.
        order.invalidate_recordset(["ativ_erechnung_pdf_file"])

    # The warning (with traceback) the feature logs for the unreadable file is
    # the expected outcome here, not noise worth a yellow or red build.
    @mute_logger("odoo.addons.ativ_greenrx.models.ir_actions_report")
    def test_corrupted_stored_file_is_stale_and_not_fatal(self):
        """A stored file that cannot be parsed counts as stale (R2, R5).

        ``_ativ_erechnung_stored_xml`` is what answers the staleness check, and
        its answer for a file it cannot read a payload out of is "does not
        match" — which has to hold for a corrupted stored file just as much as
        for one carrying no payload, because every print, download and send of
        the order goes through that check. A parse error escaping it would not
        degrade the feature, it would take the order's document down
        altogether: the print button, both mail templates and the portal
        download, each with a traceback nobody can act on, and no way out of it,
        since the unreadable bytes are precisely what the order holds.

        Both halves of the freeze rule follow from that verdict, and both are
        asserted here, because the fail-safe is only a fail-*safe* if it lands
        on the same two outcomes any other stale file gets: rebuilt while the
        Gutschrift has not been sent, refused with the feature's own message
        once it has.
        """
        order = self.order
        order.button_confirm()
        self.assertTrue(order.ativ_erechnung_pdf_file, "the confirmation stored no PDF")
        self.assertFalse(order.ativ_erechnung_sent)

        # --- before sending: the unreadable file is rebuilt
        self._corrupt_stored_pdf(order)

        rendered = self._render(order)
        # The hook stores through a sudo() environment, so both the file and the
        # chatter are re-read from the database rather than from a stale cache.
        order.invalidate_recordset(["ativ_erechnung_pdf_file", "message_ids"])

        document = self._assert_carries_facturx_for(rendered, order)
        self.assertEqual(self._buyer_name(document), self.buyer.name)
        rebuilt = self._stored_attachments(order)
        self.assertEqual(
            len(rebuilt), 1,
            "regenerating left the previous attachment behind",
        )
        self.assertEqual(
            rebuilt.raw, rendered,
            "the corrupted file was replaced for the caller but not in storage, "
            "so the next send would carry it again",
        )
        self.assertTrue(
            any("regenerated" in (body or "") for body in order.message_ids.mapped("body")),
            "no chatter note records the regenerated e-Rechnung",
        )

        # --- sending it: a mail carrying a copy of the stored PDF freezes it
        self._post_as_sent(order)
        self.assertTrue(
            order.ativ_erechnung_sent,
            "posting the e-Rechnung as an attachment did not freeze it",
        )

        # --- after sending: the render is refused, and by the feature's own
        # rule rather than by a parse error from inside the check
        self._corrupt_stored_pdf(order)

        with self.assertRaises(UserError) as capture:
            self._render(order)
        self.assertIn(order.display_name, str(capture.exception))

        order.invalidate_recordset(["ativ_erechnung_pdf_file"])
        self.assertEqual(
            self._stored_attachments(order).raw, CORRUPTED_PDF_CONTENT,
            "the frozen e-Rechnung was rebuilt",
        )

    def test_sent_order_refuses_line_changes(self):
        """The freeze covers the lines too, not just the header (R5).

        The line content is as much part of the payload as the buyer is, and it
        is funnelled through a different model, so the guard has to hold there
        as well.
        """
        order = self.order
        order.button_confirm()
        self._post_as_sent(order)
        self.assertTrue(order.ativ_erechnung_sent)

        line = order.order_line
        original_qty = line.product_qty
        with self.assertRaises(UserError):
            line.product_qty = original_qty + 1
        self.assertEqual(line.product_qty, original_qty)

        with self.assertRaises(UserError):
            line.unlink()
        self.assertTrue(line.exists())

    def test_sent_order_refuses_a_company_change(self):
        """The freeze covers the company, because the currency hangs off it (R5).

        ``company_id`` puts no value of its own into the payload, which is why
        it is the one watched field whose reason is not "the document says so":
        core's ``currency_id`` is a *stored computed* field depending on
        ``partner_id`` and ``company_id``
        (``partner.property_purchase_currency_id or company.currency_id``), and
        the write guard can only look at the fields the caller actually named.
        A write touching the company alone therefore moves the currency of a
        frozen Gutschrift in the recompute that follows it, with ``currency_id``
        never appearing in ``vals`` for the guard to see — and since the payload
        declares its currency unconditionally, what that silently produces is a
        sent document whose euro amounts the order no longer agrees are euro,
        surfacing at the earliest on the next render as a blocked download.

        The company created below carries another currency for exactly that
        reason: it makes the write one that really would change the effective
        currency, so the test is about the bypass and not merely about a
        different ``res.company`` id.
        """
        order = self.order
        order.button_confirm()
        self._post_as_sent(order)
        self.assertTrue(order.ativ_erechnung_sent)

        other_company = self.env["res.company"].create({
            "name": "ATIV Zweitgesellschaft",
            "currency_id": self.env.ref("base.USD").id,
        })
        # The premise, asserted rather than assumed: this company would move the
        # order off euro, which is what makes the write worth refusing.
        self.assertNotEqual(other_company, order.company_id)
        self.assertNotEqual(
            other_company.currency_id, self.currency_eur,
            "the second company issues in euro as well, so the write below "
            "could not change the order's currency at all",
        )
        frozen_company = order.company_id
        frozen_checksum = order.ativ_erechnung_pdf_id.checksum

        with self.assertRaises(UserError) as capture:
            order.company_id = other_company
        self.assertIn(order.display_name, str(capture.exception))

        # Re-read from the database: the point is that nothing persisted, which
        # the cache of an aborted write cannot say.
        order.invalidate_recordset(["company_id", "currency_id"])
        self.assertEqual(
            order.company_id, frozen_company,
            "the company changed on an order whose e-Rechnung was already sent",
        )
        self.assertEqual(
            order.currency_id, self.currency_eur,
            "the currency of a sent e-Rechnung moved off euro behind a write "
            "that only named the company",
        )
        self.assertEqual(
            order.ativ_erechnung_pdf_id.checksum, frozen_checksum,
            "the frozen e-Rechnung was rebuilt",
        )

    def test_internal_note_with_the_pdf_does_not_freeze_the_order(self):
        """Logging a note with the PDF attached is not a send (R7).

        The freeze is irreversible by design, so it may only fire on something
        that genuinely left the system. The web client posts "Log a note" with
        ``message_type == 'comment'``, exactly like "Send message" — the
        subtype is the only thing that tells the two apart — so a user filing a
        copy of the Gutschrift in the chatter would otherwise freeze the order
        for good with nothing having been sent to anybody.

        The second half is what keeps this from being a test of a disabled
        feature: the exclusion narrows the trigger, it does not remove it.
        """
        order = self.order
        order.button_confirm()
        self.assertTrue(order.ativ_erechnung_pdf_file, "the confirmation stored no PDF")

        order.message_post(
            body="Kopie der Gutschrift zur Ablage",
            message_type="comment",
            subtype_xmlid="mail.mt_note",
            attachments=[("Gutschrift.pdf", order.ativ_erechnung_pdf_id.raw)],
        )

        self.assertFalse(
            order.ativ_erechnung_sent,
            "an internal note froze the e-Rechnung although nothing was sent",
        )
        # The consequence that actually matters: the order is still editable,
        # and the change still rebuilds the payload.
        order.partner_id = self.buyer_other
        self.assertEqual(
            self._buyer_name(
                self._assert_carries_facturx_for(
                    order.ativ_erechnung_pdf_id.raw, order
                )
            ),
            self.buyer_other.name,
        )

        # --- and a real send freezes it after all
        self._post_as_sent(order)
        self.assertTrue(
            order.ativ_erechnung_sent,
            "sending the e-Rechnung for real no longer freezes it either",
        )

    def test_disabled_feature_is_a_no_op(self):
        """With the feature off, confirming is stock Odoo behaviour (R6).

        Switchable per installation means more than "no PDF": an installation
        that never configured an issuer must not have its purchase orders
        blocked by a validation it never asked for, so the order used here is
        one that *would* fail validation — an incomplete buyer and no issuer at
        all — and it has to confirm regardless.
        """
        self._configure_erechnung(enabled=False, issuer=self.env["res.partner"])
        order = self._create_order(self.buyer_incomplete)

        # No UserError: the validation is a pass-through when switched off.
        order.button_confirm()

        self.assertEqual(
            order.state, "purchase",
            "the order did not confirm with the feature switched off",
        )
        self.assertFalse(
            order.ativ_erechnung_pdf_file,
            "a hybrid PDF was stored with the feature switched off",
        )
        self.assertFalse(self._stored_attachments(order))
        # The render still happens on the confirmation path; what must not
        # happen is an embedded payload of ours. Core's own UBL Order XML is
        # still there, and still none of this feature's business.
        self.assertEqual(
            self._facturx_payloads(self._render(order)), [],
            "the PDF carries an e-invoice with the feature switched off",
        )

    # ------------------------------------------------------------------
    # The double-validation route to a final Gutschrift
    # ------------------------------------------------------------------

    def _user_without_approval_rights(self):
        """Return a purchase user who may confirm an order but not approve it.

        Needed because of how core decides whether a confirmation goes straight
        to ``purchase`` or stops at ``to approve``:
        ``purchase.order._approval_allowed`` is
        ``one_step or (two_step and amount < threshold) or
        has_group('purchase.group_purchase_manager')``. The environment these
        tests run in is the superuser (``base.user_root``), and
        ``purchase/security/purchase_security.xml`` puts *root itself* into
        ``group_purchase_manager`` — so switching the company to two-step
        validation is on its own not enough: the last clause would still
        short-circuit to ``True`` and ``button_confirm`` would land on
        ``purchase``, leaving ``button_approve`` unreachable and the test
        passing vacuously on the single-step path it already covers.

        Confirming as a non-manager is therefore the only way into
        ``to approve``, and it is what core's own double-validation tests do
        (``purchase/tests/test_access_rights.py::test_double_validation``,
        ``purchase_stock/tests/test_purchase_stock_report.py``). Only read
        access is exercised under this user — the heavy generation path runs on
        the approval, back in the class environment.
        """
        return self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Einkauf ohne Freigaberecht",
            "login": "ativ_erechnung_purchase_user",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("purchase.group_purchase_user").id,
            ])],
        })

    def _enable_double_validation(self):
        """Make every order of this company need a manager's approval.

        ``po_double_validation_amount = 0`` rather than a large figure: the
        threshold is compared with ``<``, so zero means even this fixture's
        ~177 € order is above it and no order can slip through as "small
        enough".
        """
        self.env.company.write({
            "po_double_validation": "two_step",
            "po_double_validation_amount": 0,
        })

    @staticmethod
    def _line_item_count(document):
        """Return how many trade line items ``document`` carries.

        One per order line (``_build_line_item``), which is what makes this a
        statement about the rebuilt *content* rather than merely about changed
        bytes.
        """
        return len(document.findall(
            ".//ram:IncludedSupplyChainTradeLineItem", namespaces=NS
        ))

    def test_approve_generates_and_revalidates_under_double_validation(self):
        """Approval is the second route to a final Gutschrift, and it guards it too (R6).

        Under double validation ``button_confirm`` does *not* make the order
        final — it parks it in ``to approve`` — so the generation that the
        single-step tests see at confirmation time has to happen here instead,
        and the validation that protects it has to run here as well. Both
        halves are asserted, because each is a way for a final Gutschrift to
        come into existence without a valid e-Rechnung:

        * the order that is approved must come out of it carrying a PDF that
          ``button_confirm`` deliberately did not build; and
        * an order whose data went bad *while it sat in the approval queue*
          must not be approvable at all. Nothing re-checks an order between
          confirmation and approval, so ``button_approve``'s own
          pre-``super()`` check is the last chance — the second half below
          breaks the data after the order reached ``to approve``, which is
          exactly the window a check placed only in ``button_confirm`` would
          miss.
        """
        self._enable_double_validation()
        requester = self._user_without_approval_rights()

        # --- confirmation parks the order, and generates nothing
        order = self.order
        order.with_user(requester).button_confirm()

        self.assertEqual(
            order.state, "to approve",
            "the order became final at confirmation time, so this test never "
            "reached the approval path it is about",
        )
        self.assertFalse(
            order.ativ_erechnung_pdf_file,
            "an order still awaiting approval already carries an e-Rechnung",
        )
        self.assertFalse(self._stored_attachments(order))

        # --- approval makes it final, and generates the e-Rechnung
        order.button_approve()

        self.assertEqual(
            order.state, "purchase",
            "the manager's approval did not make the order final",
        )
        self.assertTrue(
            order.ativ_erechnung_pdf_file,
            "approving a complete order stored no hybrid PDF, so a final "
            "Gutschrift exists without an e-Rechnung",
        )
        self.assertEqual(
            len(self._stored_attachments(order)), 1,
            "the approved order's stored hybrid PDF is not backed by exactly "
            "one attachment",
        )
        document = self._assert_carries_facturx_for(
            order.ativ_erechnung_pdf_id.raw, order
        )
        self.assertEqual(self._buyer_name(document), self.buyer.name)

        # --- the data going bad inside the approval window blocks the approval
        pending = self._create_order(self.buyer)
        pending.with_user(requester).button_confirm()
        self.assertEqual(pending.state, "to approve")

        # Broken only now: at confirmation time this order validated cleanly,
        # so a check that ran only there would have let it through.
        self.issuer.vat = False

        with self.assertRaises(UserError) as capture:
            pending.button_approve()

        message = str(capture.exception)
        self.assertIn(self.issuer.display_name, message)
        self.assertIn(
            "VAT id", message,
            "the error does not name the field that is missing",
        )
        self.assertIn(pending.display_name, message)

        self.assertEqual(
            pending.state, "to approve",
            "the order was approved although its e-invoice data is incomplete",
        )
        self.assertFalse(pending.ativ_erechnung_pdf_file)
        self.assertFalse(self._stored_attachments(pending))

    # ------------------------------------------------------------------
    # Lines added to an order that is already confirmed
    # ------------------------------------------------------------------

    def test_new_line_regenerates_then_is_refused_once_sent(self):
        """An added line rebuilds the e-invoice, until it has been sent (R2, R5).

        The counterpart of ``test_sent_order_refuses_line_changes`` on the
        other funnel: that one edits and deletes an existing line, this one
        adds a new one through ``purchase.order.line.create``. Core's own
        ``create`` is what Odoo calls when a user adds a line to an
        already-confirmed order ("Extra line with …"), so both halves of the
        freeze rule have to hold on it as well — the payload's line items and
        totals are built from exactly these rows.
        """
        order = self.order
        order.button_confirm()
        original_checksum = order.ativ_erechnung_pdf_id.checksum
        self.assertTrue(original_checksum, "the confirmation stored no PDF")
        self.assertEqual(
            self._line_item_count(
                self._assert_carries_facturx_for(
                    order.ativ_erechnung_pdf_id.raw, order
                )
            ),
            1,
            "the fixture order does not start out with a single line",
        )

        # Built fresh on every use: core's `create` mutates the vals it is
        # handed (`_prepare_add_missing_fields`), so a shared dict would make
        # the second attempt below a different call than the first.
        def line_vals():
            return {
                "order_id": order.id,
                "product_id": self.product.id,
                "name": "Zweite Position",
                "product_qty": 1.0,
                "price_unit": 12.5,
                "tax_ids": [(6, 0, self.tax.ids)],
            }

        # --- before sending: the new line goes through and rebuilds the payload
        self.env["purchase.order.line"].create(line_vals())

        self.assertEqual(len(order.order_line), 2, "the new line was not created")
        rebuilt = order.ativ_erechnung_pdf_id
        self.assertTrue(rebuilt, "the regenerated hybrid PDF was not stored")
        self.assertNotEqual(
            rebuilt.checksum, original_checksum,
            "the stored PDF did not change although a line was added",
        )
        self.assertEqual(
            len(self._stored_attachments(order)), 1,
            "regenerating left the previous attachment behind",
        )
        # The decisive assertion: the payload was rebuilt from the new lines,
        # not merely re-encoded.
        self.assertEqual(
            self._line_item_count(
                self._assert_carries_facturx_for(rebuilt.raw, order)
            ),
            2,
            "the rebuilt payload does not carry the line that was added",
        )

        # --- sending it: a mail carrying a copy of the stored PDF freezes it.
        self._post_as_sent(order)
        self.assertTrue(
            order.ativ_erechnung_sent,
            "posting the e-Rechnung as an attachment did not freeze it",
        )
        frozen_checksum = order.ativ_erechnung_pdf_id.checksum

        # --- after sending: a further line is refused, and nothing persists
        with self.assertRaises(UserError) as capture:
            self.env["purchase.order.line"].create(line_vals())
        self.assertIn(order.display_name, str(capture.exception))

        # Re-read from the database rather than trusting the cache: the point
        # is that the row was never inserted, which only a fresh read can say.
        order.invalidate_recordset(["order_line"])
        self.assertEqual(
            len(order.order_line), 2,
            "a line was added to an order whose e-Rechnung was already sent",
        )
        self.assertEqual(
            order.ativ_erechnung_pdf_id.checksum, frozen_checksum,
            "the frozen e-Rechnung was rebuilt",
        )

    # ------------------------------------------------------------------
    # The protected-fields write guard
    # ------------------------------------------------------------------

    def test_protected_fields_refuse_a_direct_write(self):
        """``ativ_erechnung_sent`` and the stored PDF cannot be written directly.

        This is the control that actually stops an RPC client from unfreezing a
        sent e-Rechnung or swapping the bytes it froze: ``readonly=True`` on the
        field only disarms the web client's form view, and a plain
        ``write()`` — which is all an XML-RPC/JSON-RPC call is — goes nowhere
        near it. So the guard in ``purchase.order.write`` is the whole of the
        protection, and it is asserted here from a recordset carrying the
        ordinary test context, with the opt-out key verified absent first:
        were it present by accident, both writes below would quietly succeed
        and this test would assert nothing.

        The guard fires on the *attempt*, which is why no setup is needed — the
        order is a draft that never sent anything, and writing the flag must
        still be refused rather than merely be a no-op.
        """
        order = self.order
        self.assertNotIn(
            "ativ_erechnung_internal_write", order.env.context,
            "the test context already carries the guard's opt-out key, so the "
            "writes below would not be guarded at all",
        )

        with self.assertRaises(UserError) as capture:
            order.write({"ativ_erechnung_sent": True})
        self.assertIn("cannot be set directly", str(capture.exception))
        self.assertFalse(
            order.ativ_erechnung_sent,
            "the freeze flag was set by a direct write",
        )

        with self.assertRaises(UserError):
            order.write({
                "ativ_erechnung_pdf_file": base64.b64encode(b"not a real pdf"),
            })
        self.assertFalse(
            order.ativ_erechnung_pdf_file,
            "the stored e-Rechnung PDF was replaced by a direct write",
        )
        self.assertFalse(self._stored_attachments(order))

        # The guard is keyed on the context and not simply always-raising:
        # without this, deleting the condition and leaving a bare `raise` would
        # keep both assertions above green while breaking the feature's own
        # writes (`_message_post_after_hook`, `_ativ_erechnung_on_relevant_change`).
        order.with_context(ativ_erechnung_internal_write=True).write({
            "ativ_erechnung_sent": True,
        })
        self.assertTrue(
            order.ativ_erechnung_sent,
            "the feature's own internal write was refused as well",
        )

    # ------------------------------------------------------------------
    # Who is rendering: the reader's timezone and the reader's access rights
    # ------------------------------------------------------------------
    #
    # The staleness check at the render chokepoint rebuilds the payload on every
    # print, download and send, and therefore runs in whatever environment asked
    # for the PDF. Two properties of that environment must not reach its verdict
    # or its outcome: *where* the reader is, which decides the issue date, and
    # *what the reader may read*, which the payload's own master data does not
    # belong to. Both tests below therefore work from an environment that is not
    # the class one, which is the only way to exercise either.

    def test_render_from_another_timezone_serves_the_stored_file(self):
        """A reader elsewhere gets the stored e-Rechnung, not a stale verdict (R4, R5).

        The issue date is resolved in the *reader's* timezone —
        ``date_order`` is a UTC datetime while the report prints it where the
        reader is, and the payload has to state the date that is on the page.
        So one and the same untouched order is two different calendar days to
        two readers either side of a day boundary, and the staleness check
        compares the rebuilt payload byte for byte: a date resolved afresh per
        reader would put "somebody else is looking" into a check that is meant
        to answer "did the data change". Rebuilding with the date the stored
        payload itself carries is what keeps the two apart.

        Both consequences are asserted, and the second is the incident: while
        the Gutschrift is unsent a re-resolved date costs a needless rebuild and
        a chatter note claiming a change nobody made, but once it has been sent
        the download is refused outright — permanently, for everybody outside
        the timezone it was confirmed in, with the data and the document in
        perfect agreement all along.
        """
        order = self.order
        # Confirmed from a fixed timezone rather than from whatever the runtime
        # user carries: what the stored file is stamped with has to be known
        # before a second timezone can be said to differ from it.
        order.with_context(tz="UTC").button_confirm()
        stored = order.ativ_erechnung_pdf_id
        original_checksum = stored.checksum
        original_bytes = stored.raw
        self.assertTrue(original_checksum, "the confirmation stored no PDF")
        self.assertEqual(
            self._issue_date(
                self._assert_carries_facturx_for(original_bytes, order)
            ),
            ISSUE_DATE_UTC,
            "the stored e-Rechnung is not stamped with the UTC date of the "
            "fixture's date_order, so the timezone is not the only thing that "
            "differs below",
        )

        far_east = self.env(context=dict(self.env.context, tz=TZ_NEXT_DAY))
        # The premise of the whole test: in that timezone this order really does
        # fall on another calendar day. Asserted rather than assumed — were it
        # not so (a changed fixture ``date_order``, a dropped timezone
        # database), everything below would pass while exercising nothing.
        self.assertNotEqual(
            fields.Datetime.context_timestamp(
                order.with_env(far_east), order.date_order
            ).strftime("%Y%m%d"),
            ISSUE_DATE_UTC,
            "the chosen timezone puts the order on the same calendar day as "
            "UTC, so this test cannot tell a re-resolved issue date from the "
            "stored one",
        )

        # --- unsent: nothing changed, so nothing may be rebuilt
        rendered = self._render(order, env=far_east)
        # The hook stores through a sudo() environment, so both the file and the
        # chatter are re-read from the database rather than from a stale cache.
        order.invalidate_recordset(["ativ_erechnung_pdf_file", "message_ids"])

        self.assertEqual(
            rendered, original_bytes,
            "the reader in another timezone was served a different document "
            "than the stored one",
        )
        self.assertEqual(
            self._stored_attachments(order).checksum, original_checksum,
            "rendering from another timezone rebuilt the stored e-Rechnung "
            "although nothing about the order, the issuer or the lines changed",
        )
        self.assertFalse(
            any(
                "regenerated" in (body or "")
                for body in order.message_ids.mapped("body")
            ),
            "the chatter tells the user the underlying data changed, and "
            "nothing did",
        )

        # --- sending it: a mail carrying a copy of the stored PDF freezes it
        self._post_as_sent(order)
        self.assertTrue(
            order.ativ_erechnung_sent,
            "posting the e-Rechnung as an attachment did not freeze it",
        )

        # --- sent: the same reader must still be able to download it
        rendered_again = self._render(order, env=far_east)

        self.assertEqual(
            rendered_again, original_bytes,
            "the sent e-Rechnung is not served unchanged to a reader in "
            "another timezone",
        )

    def _portal_user_for(self, partner):
        """Return a portal user whose contact is ``partner``.

        ``base.group_portal`` and nothing besides, because the point is what an
        external user may *not* read: core grants portal read on
        ``purchase.order`` restricted to their own orders
        (``purchase.portal_purchase_order_user_rule``) and on ``res.partner``
        restricted to their own commercial partner
        (``base.res_partner_portal_public_rule``), and no access at all to
        ``res.partner.bank``, which ``base`` opens to internal users only. That
        is exactly the shape of a vendor downloading their own Gutschrift.

        Linked to the order's own partner rather than to a fresh one: that
        record rule is what makes this the vendor of *this* Gutschrift, a reader
        entitled to the document, instead of a stranger who would be refused
        for a reason the test is not about.
        """
        return self.env["res.users"].with_context(no_reset_password=True).create({
            "login": "ativ_erechnung_portal_vendor",
            "partner_id": partner.id,
            "group_ids": [(6, 0, [self.env.ref("base.group_portal").id])],
        })

    def test_staleness_check_does_not_read_on_the_readers_behalf(self):
        """The reader's access rights decide nothing about the stored file (R4).

        The check builds the payload from the order *and* from internal master
        data — the issuer contact, its VAT id, its bank account, ``res.bank``
        for the BIC — and it runs on every print, download and send, where an
        already-generated PDF used to be handed back without reading any of
        that. Whether the stored file is still the document the data describes
        is the system's question and not the reader's, so the reads are
        elevated: a reader entitled to the document but not to the master data
        behind it — a vendor downloading their own Gutschrift from the portal,
        an internal user of another company than the issuer contact, whom the
        multi-company rule on ``res.partner`` keeps out — must not be answered
        with an ``AccessError`` about a document that is in perfect order.

        Driven on ``_ativ_erechnung_build_xml`` in the restricted user's own
        environment rather than through a whole render, because core cannot
        render *this* report as a portal user at all and does not try: the
        template reads ``product.product``, which only ``base.group_user`` may
        see, which is why ``portal._show_report`` elevates the entire report
        action before rendering anything. The access requirements of core's
        template are core's; what this feature answers for is that its own
        check adds none, and that is what the environment below pins down.
        """
        order = self.order
        order.button_confirm()

        vendor = self._portal_user_for(self.buyer)
        # The premise: the issuer's master data really is out of this reader's
        # reach. Without it a user who happened to be allowed everything would
        # make the assertion below pass while proving nothing.
        with self.assertRaises(AccessError):
            self.issuer.with_user(vendor).mapped("bank_ids.acc_number")

        payload = self.env["ir.actions.report"].with_user(
            vendor
        )._ativ_erechnung_build_xml(order.with_user(vendor))

        # Not merely "nothing was raised": the elevated read reached the
        # issuer's bank account, the one value in the payload this reader
        # cannot see for themselves.
        self.assertEqual(
            self._payment_iban(etree.fromstring(payload)), ISSUER_IBAN,
            "the payload built for a restricted reader does not carry the "
            "issuer's bank account",
        )
