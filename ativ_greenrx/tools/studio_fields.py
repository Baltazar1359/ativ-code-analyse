"""Safe reads of fields that only exist in some databases.

Several fields this project relies on are created with Odoo Studio on the
customer's database. They are absent from a fresh dev or test database, so any
code that touches them has to degrade instead of raising.
"""


def read(record, field_name, default=False):
    """Return ``record.field_name``, or ``default`` when the field is not in the registry.

    Membership in ``record._fields`` is the test, not ``hasattr``: a Studio
    field missing from this database must yield ``default``, while a field that
    does exist and raises on access has to keep raising.
    """
    if field_name in record._fields:
        return getattr(record, field_name)
    return default
