"""Tests of the ``factur-x.xml`` mapping in ``tools/facturx_builder.py``.

The mapping is a pure function, so these tests call it directly instead of
going through the report: no PDF, no attachment, no wkhtmltopdf. What they
check is the one thing the rest of the feature cannot check for itself —
whether the document it produces is a *valid* EN 16931 Cross Industry Invoice.

``test_xml_validates_against_en16931_xsd`` does that against the schema
bundled under ``tests/data/``: the Factur-X EN 16931 entry point and the three
UN/CEFACT reusable-type schemas it imports, **cut down to the aggregates this
profile uses** — the element names, their order and their cardinality exactly
as the published schema declares them, without the code-list enumerations and
the CCTS annotations of the full files. A mistyped element name — ``ram:IBAN``
for ``ram:IBANID``, a ``ram:BuyerReferencedDocument`` that does not exist —
produces perfectly well-formed XML that no German tax authority will accept,
which is exactly the class of defect a "does it parse?" assertion misses and
this one catches. Those bundled files keep the official filenames but are a
hand-reduced subset of the published release, and they validate structure
rather than code values — ``tests/data/README.md`` says exactly what they do
and do not cover.

What a schema cannot check is the text of an element it types as a plain
identifier, so the two values that decide how a reader treats the whole
document — the EN 16931 guideline URN and the "this is a credit note" type
code — are pinned by ``test_xml_is_well_formed_and_namespaced`` instead. The
final authority on conformance stays Mustang-CLI on the finished hybrid PDF,
which is a human's check on the Odoo.sh build.

The Studio fields (``x_studio_vorgang``, the Reklamationsnummer) exist on the
customer's database only. A test database has none of them, so the two
omission tests below assert the degraded shape the builder must produce here:
no element at all, rather than an empty one. Because every element those
fields feed is then absent from the document, a test database alone leaves the
whole emitting branch — and the names of the elements it writes — unexercised,
so ``test_studio_fields_are_emitted_when_present`` stands in the customer
database's place by patching the one function that answers "does this database
have the field?". Everything below that line, the element names included, is
the production code path.
"""

import os
from datetime import date, datetime
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.ativ_greenrx.tools import studio_fields
from odoo.addons.ativ_greenrx.tools.facturx_builder import build_facturx_xml

# Written out rather than imported from the builder: the point is to pin the
# namespaces the standard mandates, and a test that reads them back out of the
# code under test would follow a typo there instead of catching it.
NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "qdt": "urn:un:unece:uncefact:data:standard:QualifiedDataType:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}

# Likewise spelled out rather than imported: the guideline URN is what tells a
# reader which semantic profile the document claims, and the type code is what
# makes it a Gutschrift (UNTDID 1001, 381 = credit note) rather than an
# invoice. Both are plain text to the schema, so nothing but an equality
# against the literal the standard fixes can hold them.
EN16931_GUIDELINE_ID = "urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:en16931"
CREDIT_NOTE_TYPE_CODE = "381"

# The Factur-X 1.09 EN 16931 entry point; its three ``xs:import`` siblings sit
# next to it and lxml resolves them relative to this path.
XSD_PATH = os.path.join(os.path.dirname(__file__), "data", "Factur-X_EN16931.xsd")


@tagged("post_install", "-at_install")
class TestFacturxBuilder(TransactionCase):
    """The CII document of a complete, validation-passing Gutschrift."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        germany = cls.env.ref("base.de")

        # Every field ``purchase.order._check_ativ_erechnung_data`` requires of
        # the issuer: the builder is allowed to read them unconditionally, so a
        # partner missing one would be testing a state the feature prevents.
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
        cls.issuer_bank = cls.env["res.partner.bank"].create({
            "acc_number": "DE89370400440532013000",
            "partner_id": cls.issuer.id,
            "bank_id": bank.id,
        })

        # The buyer: only its country is validated, and the mapping emits the
        # rest of the address only when present.
        cls.buyer = cls.env["res.partner"].create({
            "name": "Apotheke GreenRX",
            "country_id": germany.id,
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

        # A fixed ``date_order`` so the issue date the mapping formats, and the
        # document it produces, are the same on every run.
        cls.order = cls.env["purchase.order"].create({
            "partner_id": cls.buyer.id,
            "date_order": datetime(2026, 1, 15, 10, 30, 0),
            "order_line": [(0, 0, {
                "product_id": cls.product.id,
                "name": "Test-Arzneimittel",
                "product_qty": 3.0,
                "price_unit": 49.5,
                "tax_ids": [(6, 0, cls.tax.ids)],
            })],
        })
        # What the report's render path hands the builder: the order's date
        # resolved to a calendar day. Which timezone resolves it is the report's
        # concern, not this mapping's — it formats the date it is given.
        cls.issue_date = cls.order.date_order.date()

        # Parsed and compiled once: an ``XMLSchema`` is reusable across any
        # number of ``validate`` calls, and compiling it per test would read
        # four files again for an identical result.
        cls.schema = etree.XMLSchema(etree.parse(XSD_PATH))

    @staticmethod
    def _with_studio_values(values):
        """Make ``values`` readable on ``purchase.order``, as the customer's DB does.

        Patches ``studio_fields.read`` — the single function that decides
        whether a Studio field exists here — and nothing else: the mapping, the
        element names it writes and the ``_as_text`` conversion all run for
        real. Any other field, and any other model, still gets the real answer.
        """
        real_read = studio_fields.read

        def read(record, field_name, default=False):
            if record._name == "purchase.order" and field_name in values:
                return values[field_name]
            return real_read(record, field_name, default)

        return patch.object(studio_fields, "read", read)

    def _build(self, order=None):
        """Return the built document of ``order``, parsed."""
        xml = build_facturx_xml(order or self.order, self.issuer, self.issue_date)
        self.assertIsInstance(xml, bytes, "the payload is attached to a PDF as bytes")
        return etree.fromstring(xml)

    def _assert_schema_valid(self, document, context_msg=""):
        """Fail with the schema's own diagnostics unless ``document`` validates.

        ``error_log`` holds the result of the last ``validate`` call, so it is
        read here, next to the assertion it explains.
        """
        self.assertTrue(
            self.schema.validate(document),
            "the factur-x.xml%s does not validate against the EN 16931 "
            "schema:\n%s" % (context_msg, self.schema.error_log),
        )

    def test_xml_is_well_formed_and_namespaced(self):
        """The payload parses, is a CII document, and claims the right profile.

        The three things every downstream reader looks at before anything else:
        the root element, the namespace declarations it carries, and the two
        identifiers that say what kind of document this is. The schema types
        both identifiers as free text, so it cannot tell the EN 16931 URN from
        a typo in it — this test is the only thing that can.
        """
        document = self._build()

        self.assertEqual(document.tag, "{%s}CrossIndustryInvoice" % NS["rsm"])
        # All four declarations belong on the root: a validator that resolves
        # them from the prefixes alone rejects a document missing one.
        self.assertEqual(
            {prefix: uri for prefix, uri in document.nsmap.items() if prefix},
            NS,
        )

        self.assertEqual(
            document.findtext(
                "rsm:ExchangedDocumentContext"
                "/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID",
                namespaces=NS,
            ),
            EN16931_GUIDELINE_ID,
            "the document does not claim the EN 16931 profile",
        )
        self.assertEqual(
            document.findtext("rsm:ExchangedDocument/ram:TypeCode", namespaces=NS),
            CREDIT_NOTE_TYPE_CODE,
            "an ATIV e-Rechnung is a Gutschrift: UNTDID 1001 code 381",
        )

    def test_xml_validates_against_en16931_xsd(self):
        """The document is a schema-valid EN 16931 Cross Industry Invoice.

        The broadest assertion available offline: one call covers every
        element name, every aggregate's child sequence, every cardinality and
        every data type of the mapping, against the schema's own declarations
        rather than against a list of names this test would have to keep in
        step with the builder.
        """
        self._assert_schema_valid(self._build())

    def test_document_number_and_totals_match_the_order(self):
        """The document identifies and totals the very order it was built from.

        A document whose figures disagree with the Odoo record it was printed
        from is worse than no document, so each total is checked against the
        order's own stored amount, formatted as the CII two-decimal amount.
        """
        document = self._build()

        self.assertEqual(
            document.findtext("rsm:ExchangedDocument/ram:ID", namespaces=NS),
            self.order.name,
        )

        summation = document.find(
            "rsm:SupplyChainTradeTransaction/ram:ApplicableHeaderTradeSettlement"
            "/ram:SpecifiedTradeSettlementHeaderMonetarySummation",
            namespaces=NS,
        )
        self.assertIsNotNone(summation, "the header monetary summation is mandatory")
        self.assertEqual(
            summation.findtext("ram:GrandTotalAmount", namespaces=NS),
            "%.2f" % self.order.amount_total,
        )
        # The same figure once more, arithmetic rather than read back out of the
        # order: 3 × 49.50 = 148.50 net, 19 % VAT = 28.22, so 176.72 gross. The
        # assertion above compares the document against the very attribute the
        # builder read, so it holds even if the order's totals are themselves
        # wrong; this one is what notices that.
        self.assertEqual(
            summation.findtext("ram:GrandTotalAmount", namespaces=NS),
            "176.72",
            "the grand total is not the fixture's 3 × 49.50 plus 19 % VAT",
        )
        self.assertEqual(
            summation.findtext("ram:LineTotalAmount", namespaces=NS),
            "%.2f" % self.order.amount_untaxed,
        )
        self.assertEqual(
            summation.findtext("ram:TaxTotalAmount", namespaces=NS),
            "%.2f" % self.order.amount_tax,
        )
        self.assertEqual(
            summation.findtext("ram:DuePayableAmount", namespaces=NS),
            "%.2f" % self.order.amount_total,
        )

        # The issue date is the caller's, formatted as UNTDID 2379 qualifier 102.
        self.assertEqual(self.issue_date, date(2026, 1, 15))
        self.assertEqual(
            document.findtext(
                "rsm:ExchangedDocument/ram:IssueDateTime/udt:DateTimeString",
                namespaces=NS,
            ),
            "20260115",
        )

    def test_discounted_line_prices_agree_with_its_total(self):
        """A discounted line's net unit price is the discounted one (R2).

        ``price_unit`` on a ``purchase.order.line`` is the *list* price, which
        is what CII calls the gross price; the discount lives in a separate
        field and is already inside ``price_subtotal``. Emitting ``price_unit``
        as the net price too therefore produces a document that contradicts its
        own line total and the PDF printed next to it — and no user action is
        needed to get there, because core fills ``discount`` in from the
        vendor's pricelist. So the net price has to be
        ``price_unit_discounted``, and the three figures have to agree:
        net × quantity = line total = the order's own ``price_subtotal``.
        """
        order = self.env["purchase.order"].create({
            "partner_id": self.buyer.id,
            "date_order": datetime(2026, 1, 15, 10, 30, 0),
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "name": "Test-Arzneimittel",
                "product_qty": 2.0,
                "price_unit": 100.0,
                "discount": 10.0,
                "tax_ids": [(6, 0, self.tax.ids)],
            })],
        })
        line = order.order_line
        # The fixture is only a test of the discount if the discount survived
        # the computes that core runs over `price_unit` and `discount`.
        self.assertEqual(line.discount, 10.0)
        self.assertEqual(line.price_unit, 100.0)
        self.assertEqual(line.price_unit_discounted, 90.0)
        self.assertEqual(line.price_subtotal, 180.0)

        document = self._build(order)
        agreement = document.find(
            "rsm:SupplyChainTradeTransaction/ram:IncludedSupplyChainTradeLineItem"
            "/ram:SpecifiedLineTradeAgreement",
            namespaces=NS,
        )
        self.assertIsNotNone(agreement, "the line carries no trade agreement")

        # BT-148: the price before the discount, which is Odoo's `price_unit`.
        self.assertEqual(
            agreement.findtext(
                "ram:GrossPriceProductTradePrice/ram:ChargeAmount", namespaces=NS
            ),
            "100.00",
            "the gross price is not the undiscounted list price",
        )
        # BT-146: the price actually charged.
        net_price = agreement.findtext(
            "ram:NetPriceProductTradePrice/ram:ChargeAmount", namespaces=NS
        )
        self.assertEqual(
            net_price, "90.00",
            "the net unit price is not the discounted price",
        )
        self.assertNotEqual(
            net_price,
            agreement.findtext(
                "ram:GrossPriceProductTradePrice/ram:ChargeAmount", namespaces=NS
            ),
            "the net and the gross price are the same figure on a discounted line",
        )
        # The actual defect this test exists for: the net price and the line
        # total have to describe the same money. Read out of the document and
        # multiplied here, rather than compared against another element, so a
        # builder that got both wrong in the same way still fails.
        line_total = document.findtext(
            "rsm:SupplyChainTradeTransaction/ram:IncludedSupplyChainTradeLineItem"
            "/ram:SpecifiedLineTradeSettlement"
            "/ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount",
            namespaces=NS,
        )
        self.assertEqual(line_total, "%.2f" % line.price_subtotal)
        self.assertAlmostEqual(
            float(net_price) * line.product_qty,
            float(line_total),
            places=2,
            msg="the net unit price times the quantity is not the line total",
        )

        # BT-147: the discount stated as the schema has it — an allowance, not
        # a charge, nested under the gross price.
        allowance = agreement.find(
            "ram:GrossPriceProductTradePrice/ram:AppliedTradeAllowanceCharge",
            namespaces=NS,
        )
        self.assertIsNotNone(
            allowance, "the line's discount is not stated as an allowance"
        )
        self.assertEqual(
            allowance.findtext("ram:ChargeIndicator/udt:Indicator", namespaces=NS),
            "false",
            "the discount is stated as a charge instead of an allowance",
        )
        self.assertEqual(
            allowance.findtext("ram:CalculationPercent", namespaces=NS), "10.00"
        )
        self.assertEqual(
            allowance.findtext("ram:BasisAmount", namespaces=NS), "100.00"
        )
        self.assertEqual(
            allowance.findtext("ram:ActualAmount", namespaces=NS), "10.00",
            "the allowance is not the money the discount takes off the unit price",
        )

        # Both prices and the allowance sit in a fixed position of the
        # agreement's sequence, so the schema has to be asked again with them.
        self._assert_schema_valid(document, " of a discounted line")

    def test_allowance_charge_omitted_on_an_undiscounted_line(self):
        """No discount means no ``ram:AppliedTradeAllowanceCharge`` (R2).

        Same omit-rather-than-blank rule as every other optional element here:
        an allowance of 0 % off 0.00 states nothing and would only invite a
        reader to subtract it twice. The fixture order carries no discount.
        """
        self.assertFalse(self.order.order_line.discount)
        document = self._build()
        self.assertEqual(
            document.findall(".//ram:AppliedTradeAllowanceCharge", namespaces=NS),
            [],
            "an allowance is emitted although the line carries no discount",
        )
        # Without a discount the two prices legitimately coincide.
        agreement = document.find(
            "rsm:SupplyChainTradeTransaction/ram:IncludedSupplyChainTradeLineItem"
            "/ram:SpecifiedLineTradeAgreement",
            namespaces=NS,
        )
        self.assertEqual(
            agreement.findtext(
                "ram:NetPriceProductTradePrice/ram:ChargeAmount", namespaces=NS
            ),
            "%.2f" % self.order.order_line.price_unit,
        )

    def test_order_without_a_real_line_is_refused(self):
        """An order of nothing but sections cannot be mapped, and says so.

        Both the schema and EN 16931 want at least one line item and at least
        one header ``ram:ApplicableTradeTax``, and the builder derives the
        latter from the very lines it has just filtered the sections out of. A
        document built from such an order would be invalid, so the builder
        refuses it by name instead of emitting it.
        """
        order = self.env["purchase.order"].create({
            "partner_id": self.buyer.id,
            "date_order": datetime(2026, 1, 15, 10, 30, 0),
            # A section line still has to satisfy the columns core declares
            # required on every line -- ``product_qty`` is a plain
            # ``required=True`` Float with no default, so leaving it out makes
            # the INSERT fail on the NOT NULL constraint long before the
            # builder this test is about is reached. Same vals core's own
            # section-line fixture uses (``purchase``'s
            # ``test_purchase_order_report``).
            "order_line": [(0, 0, {
                "display_type": "line_section",
                "name": "Nur eine Zwischenueberschrift",
                "product_id": False,
                "product_qty": 0.0,
                "product_uom_id": False,
                "price_unit": 0.0,
                "tax_ids": False,
            })],
        })

        with self.assertRaises(UserError) as capture:
            build_facturx_xml(order, self.issuer, self.issue_date)

        self.assertIn(
            order.display_name, str(capture.exception),
            "the error does not name the order that cannot be mapped",
        )

    def test_buyer_reference_omitted_when_vorgang_absent(self):
        """No Vorgang means no ``ram:BuyerReference`` — not an empty one.

        ``x_studio_vorgang`` does not exist in this database, which is the
        situation of every dev and staging build. An empty ``ram:BuyerReference``
        would be an EN 16931 violation, so the element has to disappear with its
        value.
        """
        self.assertNotIn(
            "x_studio_vorgang",
            self.env["purchase.order"]._fields,
            "this test asserts the behaviour for an ABSENT Studio field",
        )
        document = self._build()
        self.assertEqual(
            document.findall(".//ram:BuyerReference", namespaces=NS),
            [],
            "ram:BuyerReference is emitted although no Vorgang is available",
        )

    def test_additional_referenced_document_omitted_when_complaint_number_absent(self):
        """No Reklamationsnummer means no ``ram:AdditionalReferencedDocument``.

        Same contract as the Vorgang above, for the other reference: the
        aggregate carries nothing but the complaint number and its type code, so
        without a number there is nothing to wrap.
        """
        for field_name in ("x_studio_complaint_number", "x_studio_ticket_1"):
            self.assertNotIn(
                field_name,
                self.env["purchase.order"]._fields,
                "this test asserts the behaviour for an ABSENT Studio field",
            )
        document = self._build()
        self.assertEqual(
            document.findall(".//ram:AdditionalReferencedDocument", namespaces=NS),
            [],
            "ram:AdditionalReferencedDocument is emitted although no "
            "Reklamationsnummer is available",
        )

    def test_studio_fields_are_emitted_when_present(self):
        """With the Studio fields readable, both references land in the document.

        The counterpart of the two omission tests, and the only test that
        exercises the branch the customer's database actually takes. Without
        it, every element the Studio values feed is absent from every document
        the test suite builds, and a wrong element name there — the
        ``ram:BuyerReferencedDocument`` that does not exist, for one — reaches
        production unseen: no assertion and no schema check can object to an
        element that is never written.
        """
        values = {
            "x_studio_vorgang": "VG-2026-0042",
            "x_studio_complaint_number": "RKL-7781",
            "x_gutschrift_verwendungszweck": "Gutschrift VG-2026-0042",
        }
        with self._with_studio_values(values):
            document = self._build()

        agreement = (
            "rsm:SupplyChainTradeTransaction/ram:ApplicableHeaderTradeAgreement"
        )
        self.assertEqual(
            document.findtext("%s/ram:BuyerReference" % agreement, namespaces=NS),
            values["x_studio_vorgang"],
            "the Vorgang is not carried as the buyer's reference",
        )
        referenced = document.find(
            "%s/ram:AdditionalReferencedDocument" % agreement, namespaces=NS
        )
        self.assertIsNotNone(
            referenced, "the Reklamationsnummer is not carried at all"
        )
        self.assertEqual(
            referenced.findtext("ram:IssuerAssignedID", namespaces=NS),
            values["x_studio_complaint_number"],
        )
        # UNTDID 1153 code 130: the reference has no untyped form.
        self.assertEqual(referenced.findtext("ram:TypeCode", namespaces=NS), "130")

        # Where the customer's database carries a Verwendungszweck, it is the
        # payment reference instead of the Gutschrift number the absent case
        # falls back to.
        self.assertEqual(
            document.findtext(
                "rsm:SupplyChainTradeTransaction/ram:ApplicableHeaderTradeSettlement"
                "/ram:SpecifiedTradePaymentTerms/ram:Description",
                namespaces=NS,
            ),
            values["x_gutschrift_verwendungszweck"],
            "the Verwendungszweck is not carried as the payment reference",
        )

        # Both elements sit in a fixed position of their aggregate's sequence,
        # so the schema has to be asked again with them present.
        self._assert_schema_valid(document, " with both Studio references")

    def test_studio_field_patch_does_not_leak_to_other_reads(self):
        """The patch of the previous test answers for its own fields only.

        Cheap insurance on the one piece of scaffolding these tests own: a
        helper that returned a value for every field asked of it would make
        ``test_studio_fields_are_emitted_when_present`` pass no matter what the
        builder reads.
        """
        with self._with_studio_values({"x_studio_vorgang": "VG-1"}):
            order = self.order
            self.assertEqual(studio_fields.read(order, "x_studio_vorgang"), "VG-1")
            self.assertFalse(studio_fields.read(order, "x_studio_something_else"))
            self.assertEqual(
                studio_fields.read(order, "name", default="unused"), order.name
            )
        self.assertFalse(studio_fields.read(self.order, "x_studio_vorgang"))
