"""The ``factur-x.xml`` payload of an ATIV Gutschrift.

Pure, stateless mapping of a ``purchase.order`` onto a Cross Industry Invoice
(CII) document, the XML half of a hybrid Factur-X / ZUGFeRD PDF. It reads
fields off the recordsets it is handed and writes nothing: no ORM write, no
database access beyond those reads, so the caller owns the transaction. The one
error it reports itself is the order it cannot map at all — no line to map —
because only here is that visible, and a ``UserError`` is what the rest of the
feature raises for a Gutschrift that must not be built.

The issuer's completeness (name, address, VAT id, company registry, phone,
email, bank account with IBAN and BIC) is validated by
``purchase.order._check_ativ_erechnung_data`` before an order can become final,
and therefore before this builder ever runs — the mapping below reads those
fields without re-checking them. Everything that is genuinely optional (the
buyer's VAT id, the Studio fields, a product's internal reference) is emitted
only when it has a value: an empty CII element is a schema violation, a missing
one is not.

Element *order* inside each CII aggregate is fixed by the D16B schema
sequences, which is why some elements are built in a different order than the
functional spec lists them (the buyer's reference before both trade parties,
the seller's contact between its legal organisation and its address, the
payment terms after the tax breakdown, …).
"""

import datetime
import logging

from lxml import etree

from odoo.addons.ativ_greenrx.tools import studio_fields
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# The four standard Factur-X / CII namespaces, fixed by the specification.
# ``qdt`` carries no element of this mapping but belongs to the document's
# declarations, as every Factur-X sample and validator expects it.
NSMAP = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "qdt": "urn:un:unece:uncefact:data:standard:QualifiedDataType:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}

# EN 16931 (the European semantic standard) in its Factur-X 1.0 binding: the
# profile this document claims to comply with.
GUIDELINE_ID = "urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:en16931"

# UNTDID 1001: 381 = credit note. An ATIV e-Rechnung is a Gutschrift.
DOCUMENT_TYPE_CODE = "381"

# UNTDID 2379 date format qualifier 102 = CCYYMMDD, no separators.
DATE_FORMAT_102 = "102"

# The same format as a ``strftime``/``strptime`` pattern. Named rather than
# spelled out at the one place that writes it, because the issue date is also
# read back *out* of a stored payload (``ir.actions.report``
# ``_ativ_erechnung_stored_issue_date``), and a writer and a reader that
# disagree about the format would make every comparison of the two fail.
DATE_PATTERN_102 = "%Y%m%d"

# Hardcoded per the requirements: ATIV invoices in euro only.
CURRENCY_CODE = "EUR"

# UNTDID 4461: 58 = SEPA credit transfer. The issuer's IBAN and BIC are the
# only payment data this feature knows about, so the means is never anything
# else, and the code list offers no "unknown" value worth emitting instead.
PAYMENT_MEANS_CODE = "58"

# UNTDID 5153: VAT is the only tax this mapping deals with.
TAX_TYPE_CODE = "VAT"

# UNTDID 1153: 130 = "invoicing data sheet", the reference type a buyer-side
# document number (here the Reklamationsnummer) is carried under. Mandatory in
# ``ram:AdditionalReferencedDocument``, which has no untyped form.
ADDITIONAL_DOCUMENT_TYPE_CODE = "130"

# UNTDID 5305 tax category. Rate-driven, which is a deliberate simplification:
# the data carries a percentage and nothing that would distinguish a zero rate
# from a reverse charge (AE), an exempt supply (E) or an intra-community
# delivery (K). Anything beyond "standard rate" / "zero rated" needs a tax
# category on the Odoo tax to map from.
TAX_CATEGORY_STANDARD = "S"
TAX_CATEGORY_ZERO_RATED = "Z"

# UN/ECE Recommendation 20: C62 = "one", the dimensionless unit. Used for every
# line because mapping Odoo's ``uom.uom`` records onto UN/CEFACT unit codes
# needs a mapping table that is out of this feature's scope.
DEFAULT_UNIT_CODE = "C62"

# Amounts and percentages: two decimals, as the monetary totals of the order.
AMOUNT_DIGITS = 2
# Quantities: four decimals, the precision EN 16931 allows for BT-129, so a
# fractional quantity survives the mapping.
QUANTITY_DIGITS = 4

# Distinguishes "this Studio field does not exist in this database" from "it
# exists and is empty". Both yield no element, but only the first is worth a log
# line, and only once per field per process — this runs once per order and once
# per order line, so logging per read would drown the test output.
_ABSENT = object()
_logged_absent_fields = set()


def _studio_value(record, field_name):
    """Return ``record.field_name``, or ``None`` when it is absent or empty.

    Studio fields live on the customer's database only; a dev, staging or test
    database has to produce a valid document without them.
    """
    value = studio_fields.read(record, field_name, default=_ABSENT)
    if value is _ABSENT:
        if field_name not in _logged_absent_fields:
            _logged_absent_fields.add(field_name)
            _logger.info(
                "Studio field %s.%s is absent from this database: the e-invoice "
                "is built without it.",
                record._name,
                field_name,
            )
        return None
    # An empty value is as unemittable as a missing field, and the caller only
    # ever asks "is there something to write here?".
    return value or None


def _as_text(value):
    """Return the XML text of a Studio value of unknown type.

    The Studio fields this mapping reads are typed on the customer's database
    and nowhere else: a reference can be a ``Char`` on one and a ``Many2one`` on
    another. A recordset must not reach the XML as its ``repr``, hence the
    ``display_name``. A ``Many2many`` or a one-to-many on one database where
    another has a single record yields more than one record, which
    ``display_name`` refuses to answer for, so those are joined instead.
    """
    if hasattr(value, "_name"):
        if len(value) > 1:
            return ", ".join(value.mapped("display_name"))
        return value.display_name
    if isinstance(value, datetime.date):
        # Covers ``datetime`` too, which subclasses ``date``.
        return value.strftime("%Y-%m-%d")
    return str(value)


def _decimal(value, digits=AMOUNT_DIGITS):
    """Return ``value`` as a fixed-point decimal string, the CII amount format."""
    return "%.*f" % (digits, value or 0.0)


def _sub(parent, tag, text=None, attrib=None):
    """Append ``<prefix:tag>`` to ``parent``, with ``tag`` as ``"prefix:local"``."""
    prefix, _, local = tag.partition(":")
    node = etree.SubElement(parent, "{%s}%s" % (NSMAP[prefix], local), attrib or {})
    if text is not None:
        node.text = text
    return node


def _sub_if(parent, tag, text, attrib=None):
    """Append the element only when ``text`` has a value; return it or ``None``.

    An empty ``ram:ID`` or ``ram:Description`` breaks EN 16931 validation, so
    "no value" has to mean "no element" everywhere in this mapping.
    """
    if not text:
        return None
    return _sub(parent, tag, text, attrib)


def _tax_rate(line):
    """Return the VAT percentage of ``line``, 0 when it carries no tax.

    Only the first tax is looked at: an ATIV Gutschrift line carries a single
    VAT rate, and the CII line can hold exactly one ``RateApplicablePercent``.
    """
    taxes = line.tax_ids
    return round(taxes[:1].amount, AMOUNT_DIGITS) if taxes else 0.0


def _tax_category_code(rate):
    """Return the UNTDID 5305 category of ``rate`` (see ``TAX_CATEGORY_*``)."""
    return TAX_CATEGORY_STANDARD if rate else TAX_CATEGORY_ZERO_RATED


def _build_trade_address(parent, partner):
    """Append the ``ram:PostalTradeAddress`` of ``partner`` to ``parent``.

    Shared by both trade parties: the buyer's street or zip code can be empty
    (only its country is validated), so every line is conditional.
    """
    address = _sub(parent, "ram:PostalTradeAddress")
    _sub_if(address, "ram:PostcodeCode", partner.zip)
    _sub_if(address, "ram:LineOne", partner.street)
    _sub_if(address, "ram:CityName", partner.city)
    _sub_if(address, "ram:CountryID", partner.country_id.code)
    return address


def _build_seller_party(parent, issuer):
    """Append the ``ram:SellerTradeParty`` of ``issuer`` to ``parent``."""
    seller = _sub(parent, "ram:SellerTradeParty")
    _sub(seller, "ram:Name", issuer.name)
    # The HRB number of the Handelsregister.
    legal = _sub(seller, "ram:SpecifiedLegalOrganization")
    _sub_if(legal, "ram:ID", issuer.company_registry)
    # Both are validated present by ``_check_ativ_erechnung_data``, and the
    # schema puts the contact between the legal organisation and the address.
    # Still conditional: an aggregate with no child is a schema violation, so
    # a database that somehow lacks both drops the contact rather than the
    # whole document.
    if issuer.phone or issuer.email:
        contact = _sub(seller, "ram:DefinedTradeContact")
        if issuer.phone:
            phone = _sub(contact, "ram:TelephoneUniversalCommunication")
            _sub(phone, "ram:CompleteNumber", issuer.phone)
        if issuer.email:
            email = _sub(contact, "ram:EmailURIUniversalCommunication")
            _sub(email, "ram:URIID", issuer.email)
    _build_trade_address(seller, issuer)
    tax_registration = _sub(seller, "ram:SpecifiedTaxRegistration")
    _sub(tax_registration, "ram:ID", issuer.vat, {"schemeID": "VA"})
    return seller


def _build_buyer_party(parent, partner):
    """Append the ``ram:BuyerTradeParty`` of ``partner`` to ``parent``."""
    buyer = _sub(parent, "ram:BuyerTradeParty")
    _sub(buyer, "ram:Name", partner.name)
    _build_trade_address(buyer, partner)
    if partner.vat:
        tax_registration = _sub(buyer, "ram:SpecifiedTaxRegistration")
        _sub(tax_registration, "ram:ID", partner.vat, {"schemeID": "VA"})
    return buyer


def _build_line_description(line):
    """Return the batch/expiry free text of ``line``, or ``None``.

    Factur-X has no batch number or expiry date of its own, so both go into the
    line's description. Whichever of the two is missing is left out rather than
    printed as an empty label.
    """
    ticket = _studio_value(line, "x_studio_ticket")
    # The field is a relation on the customer's database, but a plain ``Char``
    # on another: a string carries no batch number or expiry to read, and
    # reading one off it would raise rather than degrade.
    if not ticket or not hasattr(ticket, "_name"):
        return None
    parts = []
    batch_number = _studio_value(ticket, "x_studio_batch_number")
    if batch_number:
        parts.append("Batch: %s" % _as_text(batch_number))
    expiration_date = _studio_value(ticket, "x_studio_expiration_date")
    if expiration_date:
        parts.append("Expiry: %s" % _as_text(expiration_date))
    return ", ".join(parts) or None


def _build_line_item(parent, line, line_number):
    """Append the ``ram:IncludedSupplyChainTradeLineItem`` of ``line``.

    ``line_number`` is the line's 1-based position in the document, not its
    database id: the id leaks internals and is not stable across databases.
    """
    item = _sub(parent, "ram:IncludedSupplyChainTradeLineItem")

    document = _sub(item, "ram:AssociatedDocumentLineDocument")
    _sub(document, "ram:LineID", str(line_number))

    product = _sub(item, "ram:SpecifiedTradeProduct")
    # The product's internal reference, which this project uses for the PZN.
    _sub_if(product, "ram:SellerAssignedID", line.product_id.default_code)
    # A line without a product (a downpayment line, for instance) still has a
    # label: ``name`` is required on the line, so it is always a real string.
    _sub(product, "ram:Name", line.product_id.name or line.name)
    _sub_if(product, "ram:Description", _build_line_description(line))

    agreement = _sub(item, "ram:SpecifiedLineTradeAgreement")
    # The two unit prices CII distinguishes, and Odoo has both: the gross price
    # is the list price *before* the line's discount (``price_unit``), the net
    # price the one actually charged (``price_unit_discounted``, core's
    # ``price_unit * (1 - discount / 100)``). Emitting ``price_unit`` as both
    # would contradict the ``LineTotalAmount`` below, which comes from the
    # discount-aware ``price_subtotal``, and therefore the printed PDF.
    gross_price = _sub(agreement, "ram:GrossPriceProductTradePrice")
    _sub(gross_price, "ram:ChargeAmount", _decimal(line.price_unit))
    # The discount itself, in the only shape the schema has for it: an
    # allowance (``ChargeIndicator`` false, as opposed to a charge) nested
    # under the gross price, after its ``ChargeAmount`` in the sequence. An
    # undiscounted line has no allowance to state, so the whole sub-element
    # goes rather than being emitted with zeroes.
    if line.discount:
        allowance = _sub(gross_price, "ram:AppliedTradeAllowanceCharge")
        indicator = _sub(allowance, "ram:ChargeIndicator")
        _sub(indicator, "udt:Indicator", "false")
        _sub(allowance, "ram:CalculationPercent", _decimal(line.discount))
        _sub(allowance, "ram:BasisAmount", _decimal(line.price_unit))
        _sub(
            allowance,
            "ram:ActualAmount",
            _decimal(line.price_unit - line.price_unit_discounted),
        )
    net_price = _sub(agreement, "ram:NetPriceProductTradePrice")
    _sub(net_price, "ram:ChargeAmount", _decimal(line.price_unit_discounted))

    delivery = _sub(item, "ram:SpecifiedLineTradeDelivery")
    _sub(
        delivery,
        "ram:BilledQuantity",
        _decimal(line.product_qty, QUANTITY_DIGITS),
        {"unitCode": DEFAULT_UNIT_CODE},
    )

    settlement = _sub(item, "ram:SpecifiedLineTradeSettlement")
    rate = _tax_rate(line)
    trade_tax = _sub(settlement, "ram:ApplicableTradeTax")
    _sub(trade_tax, "ram:TypeCode", TAX_TYPE_CODE)
    _sub(trade_tax, "ram:CategoryCode", _tax_category_code(rate))
    _sub(trade_tax, "ram:RateApplicablePercent", _decimal(rate))
    # Mandatory in the schema's sequence, and the line's own net total: taken
    # from the stored ``price_subtotal`` so any discount is already in it.
    summation = _sub(settlement, "ram:SpecifiedTradeSettlementLineMonetarySummation")
    _sub(summation, "ram:LineTotalAmount", _decimal(line.price_subtotal))
    return item


def _build_header_agreement(parent, order, issuer):
    """Append the ``ram:ApplicableHeaderTradeAgreement`` of ``order``."""
    agreement = _sub(parent, "ram:ApplicableHeaderTradeAgreement")

    # BT-10, the buyer's own reference for the transaction: a plain text
    # element, and the first of the aggregate's sequence — hence built before
    # the two trade parties rather than next to the other references below.
    vorgang = _studio_value(order, "x_studio_vorgang")
    if vorgang is not None:
        _sub(agreement, "ram:BuyerReference", _as_text(vorgang))

    _build_seller_party(agreement, issuer)
    _build_buyer_party(agreement, order.partner_id)

    # The complaint number moved field between Studio revisions; the older name
    # is the fallback, so both shapes of the customer's database map.
    complaint_number = _studio_value(order, "x_studio_complaint_number")
    if complaint_number is None:
        complaint_number = _studio_value(order, "x_studio_ticket_1")
    if complaint_number is not None:
        referenced = _sub(agreement, "ram:AdditionalReferencedDocument")
        _sub(referenced, "ram:IssuerAssignedID", _as_text(complaint_number))
        _sub(referenced, "ram:TypeCode", ADDITIONAL_DOCUMENT_TYPE_CODE)
    return agreement


def _build_payment_means(parent, issuer):
    """Append the ``ram:SpecifiedTradeSettlementPaymentMeans`` of ``issuer``.

    The bank account is resolved exactly as the validation that guaranteed it
    exists did: the first active one.
    """
    bank = issuer.bank_ids.filtered("active")[:1]
    payment_means = _sub(parent, "ram:SpecifiedTradeSettlementPaymentMeans")
    _sub(payment_means, "ram:TypeCode", PAYMENT_MEANS_CODE)
    # Both wrappers hold nothing but the value below them, so a missing value
    # takes its wrapper with it rather than leaving an empty aggregate behind.
    if bank.acc_number:
        account = _sub(payment_means, "ram:PayeePartyCreditorFinancialAccount")
        _sub(account, "ram:IBANID", bank.acc_number)
    if bank.bank_id.bic:
        institution = _sub(payment_means, "ram:PayeeSpecifiedCreditorFinancialInstitution")
        _sub(institution, "ram:BICID", bank.bank_id.bic)
    return payment_means


def _build_header_tax(parent, lines):
    """Append one ``ram:ApplicableTradeTax`` per distinct VAT rate of ``lines``.

    The basis of a rate is the sum of the stored line subtotals at that rate,
    never a recomputed quantity times price: the subtotal already carries the
    line's discount. Its tax is the sum of the stored ``price_tax`` of the same
    lines, for the same reason and one more: BR-CO-14 wants the per-rate tax
    amounts to add up to ``TaxTotalAmount``, and summing the very per-line
    figures Odoo totalled the order from makes that true by construction, where
    a fresh ``basis * rate / 100`` drifts by a cent against per-line rounding.
    The rates keep their order of first appearance so the same order always
    yields the same document.
    """
    totals_per_rate = {}
    for line in lines:
        rate = _tax_rate(line)
        basis, tax = totals_per_rate.get(rate, (0.0, 0.0))
        totals_per_rate[rate] = (basis + line.price_subtotal, tax + line.price_tax)
    for rate, (basis, tax) in totals_per_rate.items():
        if not rate:
            # Accepted limitation, logged so support can see it happened: the
            # data carries a percentage and nothing else, so a zero rate on a
            # German domestic Gutschrift — reverse charge, an exempt supply or
            # an intra-community delivery — is indistinguishable here, and all
            # three leave as category Z.
            _logger.warning(
                "Gutschrift line(s) at a 0%% VAT rate: emitted as category %s. "
                "Reverse charge (AE), exempt (E) and intra-community (K) cannot "
                "be told apart from the rate alone.",
                TAX_CATEGORY_ZERO_RATED,
            )
        trade_tax = _sub(parent, "ram:ApplicableTradeTax")
        _sub(trade_tax, "ram:CalculatedAmount", _decimal(tax))
        _sub(trade_tax, "ram:TypeCode", TAX_TYPE_CODE)
        _sub(trade_tax, "ram:BasisAmount", _decimal(basis))
        _sub(trade_tax, "ram:CategoryCode", _tax_category_code(rate))
        _sub(trade_tax, "ram:RateApplicablePercent", _decimal(rate))


def _build_header_settlement(parent, order, issuer, lines):
    """Append the ``ram:ApplicableHeaderTradeSettlement`` of ``order``."""
    settlement = _sub(parent, "ram:ApplicableHeaderTradeSettlement")
    _sub(settlement, "ram:InvoiceCurrencyCode", CURRENCY_CODE)
    _build_payment_means(settlement, issuer)
    _build_header_tax(settlement, lines)

    # BR-CO-25: a document with something left to pay must say something about
    # its payment terms, so this aggregate is never omitted. The Studio field
    # carries the customer's own Verwendungszweck where it exists; where it does
    # not, the Gutschrift number is the reference a payer would quote anyway.
    payment_reference = _studio_value(order, "x_gutschrift_verwendungszweck")
    terms = _sub(settlement, "ram:SpecifiedTradePaymentTerms")
    _sub(
        terms,
        "ram:Description",
        _as_text(payment_reference) if payment_reference is not None else order.name,
    )

    # Every total comes from the order's own stored amounts: a document whose
    # totals disagree with the Odoo record it was printed from is worse than no
    # document, so none of them is recomputed here.
    summation = _sub(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    _sub(summation, "ram:LineTotalAmount", _decimal(order.amount_untaxed))
    _sub(summation, "ram:TaxBasisTotalAmount", _decimal(order.amount_untaxed))
    _sub(
        summation,
        "ram:TaxTotalAmount",
        _decimal(order.amount_tax),
        {"currencyID": CURRENCY_CODE},
    )
    _sub(summation, "ram:GrandTotalAmount", _decimal(order.amount_total))
    _sub(summation, "ram:DuePayableAmount", _decimal(order.amount_total))
    return settlement


def build_facturx_xml(order, issuer, issue_date):
    """Return the ``factur-x.xml`` of ``order`` as UTF-8 encoded bytes.

    :param order: a single ``purchase.order``, the Gutschrift to map.
    :param issuer: a single ``res.partner``, the seller party; already
        validated complete by ``purchase.order._check_ativ_erechnung_data``.
    :param issue_date: a ``datetime.date``, the document's issue date — the
        caller's, because ``order.date_order`` is a UTC ``Datetime`` while the
        printed report renders it in the user's timezone, and a late-evening
        order would otherwise carry one calendar day on the PDF and another in
        the XML. This mapping formats the date it is given and reads
        ``date_order`` nowhere.
    :return: a well-formed ``rsm:CrossIndustryInvoice`` document, EN 16931
        profile, ready to be attached to the hybrid PDF.
    :raises UserError: when ``order`` has no line that can be mapped at all.
    """
    order.ensure_one()
    issuer.ensure_one()
    # Section and note lines carry no product, quantity or price: they have no
    # counterpart in a CII line item list.
    lines = order.order_line.filtered(lambda line: not line.display_type)
    # Both the schema and EN 16931 require at least one line item and at least
    # one header ``ram:ApplicableTradeTax``, and the latter is derived from
    # these lines: an order made of nothing but sections and notes would come
    # out as a document no validator accepts. Refused by name rather than
    # emitted invalid — an unreadable e-Rechnung in a customer's inbox costs
    # more than a blocked print.
    if not lines:
        raise UserError(order.env._(
            "The e-Rechnung of %(name)s cannot be built: the order carries no "
            "line other than sections and notes.",
            name=order.display_name,
        ))

    root = etree.Element("{%s}CrossIndustryInvoice" % NSMAP["rsm"], nsmap=NSMAP)

    context = _sub(root, "rsm:ExchangedDocumentContext")
    guideline = _sub(context, "ram:GuidelineSpecifiedDocumentContextParameter")
    _sub(guideline, "ram:ID", GUIDELINE_ID)

    document = _sub(root, "rsm:ExchangedDocument")
    _sub(document, "ram:ID", order.name)
    _sub(document, "ram:TypeCode", DOCUMENT_TYPE_CODE)
    issue_date_time = _sub(document, "ram:IssueDateTime")
    # The caller's date, already resolved to the timezone the report prints in,
    # formatted as the CCYYMMDD of qualifier 102.
    _sub(
        issue_date_time,
        "udt:DateTimeString",
        issue_date.strftime(DATE_PATTERN_102),
        {"format": DATE_FORMAT_102},
    )

    transaction = _sub(root, "rsm:SupplyChainTradeTransaction")
    for line_number, line in enumerate(lines, start=1):
        _build_line_item(transaction, line, line_number)
    _build_header_agreement(transaction, order, issuer)
    # Required by the schema sequence between the agreement and the settlement;
    # this feature ships no delivery data, so it stays empty.
    _sub(transaction, "ram:ApplicableHeaderTradeDelivery")
    _build_header_settlement(transaction, order, issuer, lines)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
