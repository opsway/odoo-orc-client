"""Postgres immutability trigger shared by both audit logs.

A single ``orc_audit_immutable()`` function raises on any UPDATE or
DELETE and is bound to each audit-log table by its own
``BEFORE UPDATE OR DELETE`` trigger. The function is installed
idempotently (``CREATE OR REPLACE``); each table's trigger is dropped
and recreated on every install/upgrade so a schema change cleanly
re-establishes the binding.

The pruning cron in ``orc_api_access_log.py`` bypasses the trigger for
its own transaction only via ``SET LOCAL session_replication_role =
replica``. The trigger stays in place for everyone else.
"""

_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION orc_audit_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'orc audit log is append-only (table=%, op=%)',
                    TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""


def install_immutable_trigger(cr, table):
    """Bind the immutability trigger to ``table``.

    Safe to call repeatedly: function uses ``CREATE OR REPLACE``;
    trigger is dropped first then recreated.
    """
    cr.execute(_FUNCTION_DDL)
    trigger_name = "%s_immutable_trg" % table
    cr.execute(
        "DROP TRIGGER IF EXISTS %s ON %s" % (trigger_name, table)
    )
    cr.execute(
        "CREATE TRIGGER %s BEFORE UPDATE OR DELETE ON %s "
        "FOR EACH ROW EXECUTE PROCEDURE orc_audit_immutable()"
        % (trigger_name, table)
    )
