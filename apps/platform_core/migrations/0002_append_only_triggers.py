"""Enforce append-only on the three ledgers at the database level.

A trigger rather than revoked UPDATE/DELETE grants. Grants are bypassed by
superusers and by anything running as the table owner, which in practice
includes migrations and most development setups — so a grant-based rule is one
`psql` session away from not being a rule at all. A ``BEFORE UPDATE OR DELETE``
trigger holds regardless of role.

``TRUNCATE`` deliberately still works: it fires statement-level TRUNCATE
triggers only, not row-level ones, and Django's test teardown truncates. The
ledgers stay testable without a hole in the guarantee, since nothing in the
application ever issues a TRUNCATE.
"""

from django.db import migrations

LEDGER_TABLES = ["commitment_entry", "cost_entry", "stock_move"]

CREATE_FUNCTION = """
CREATE OR REPLACE FUNCTION refuse_ledger_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'Table %.% is an append-only ledger; % is refused. '
        'Post a compensating entry instead.',
        TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

DROP_FUNCTION = "DROP FUNCTION IF EXISTS refuse_ledger_mutation();"


def _create_trigger(table: str) -> str:
    return f"""
    CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION refuse_ledger_mutation();
    """


def _drop_trigger(table: str) -> str:
    return f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"


class Migration(migrations.Migration):

    dependencies = [("platform_core", "0001_initial")]

    operations = [
        migrations.RunSQL(sql=CREATE_FUNCTION, reverse_sql=DROP_FUNCTION),
        *[
            migrations.RunSQL(
                sql=_create_trigger(table), reverse_sql=_drop_trigger(table)
            )
            for table in LEDGER_TABLES
        ],
    ]
