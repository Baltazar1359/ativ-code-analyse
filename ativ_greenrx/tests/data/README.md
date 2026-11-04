# What these XSD files are — and what they are not

These four files carry the official Factur-X 1.09 EN 16931 filenames, because the
`xs:import`/`schemaLocation` cross-references between them (and the test that loads the entry
point) resolve each other by name. The **content is not** the vendor-shipped release: it is a
**hand-reduced subset**, cut down to only the aggregates, elements and data types that
`tools/facturx_builder.py` actually emits, without the CCTS annotations of the full files.

What that means for the one test that uses them
(`test_xml_validates_against_en16931_xsd`, and the second schema check in
`test_studio_fields_are_emitted_when_present`):

- It validates the document's **structure** — element names and spelling, the child sequence of
  each aggregate, cardinality, and the base data types.
- It does **not** validate **code values**. Every `qdt` code-list type here is intentionally
  left unenumerated (a bare `xs:restriction base="xs:token"`), so an invalid currency code, tax
  category code or payment-means code passes this schema unnoticed.

The authoritative conformance check is **Mustang-CLI run against a real generated hybrid PDF**.
It needs Java, so it is a human **release-checklist** step rather than a build gate — named as
such in the run's `plan.md` ("Human-checked gaps") and in `review.md`.
