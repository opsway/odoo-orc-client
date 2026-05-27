"""Sanitize ORC addon configuration on upgrade — forces re-provision
under the post-two-namespace ORC schema.

Context: the AI Workplace ("ORC") server-side cutover splits its
`users` table into two kinds (`org_user`, `platform_user`) and
enforces `org_api_tokens.infrastructure_id NOT NULL` from
migration 090 forward.  Bearer tokens minted before that
migration were issued under a looser schema and aren't tied to a
specific infrastructure row at write time — the post-090 server
still accepts them (the backfill set infrastructure_id from
engine joins) but operators upgrading the addon on a stage or
dev Odoo benefit from a clean re-provision under the new schema
rather than carrying a pre-cutover Bearer forward.

This migration clears the four `ir.config_parameter` rows the
addon writes:

    orc.endpoint_url     — provisioning endpoint
    orc.rotation_days    — key rotation interval
    orc.org_token        — Bearer for addon → ORC calls
    orc.infrastructure_id — UUID of THIS Odoo's row in ORC

After the migration runs, the addon has no local token and no
endpoint pinned.  On the next user-enrollment / systray-click,
the addon prompts the operator to re-provision (set endpoint,
mint a fresh token, register the infrastructure under the post-
090 schema).  This is the intended state.

Why this is safe to run on every upgrade through 18.0.1.12.0:

- Each `orc.*` key is rewritten by the addon during provision —
  the rows aren't user-authored, they're addon-managed.
- Re-provision is idempotent on the ORC side: the same Odoo
  registering twice updates the existing infrastructure row.

Why this only runs once: pre-migrate.py runs only when Odoo's
`base_module_upgrade` detects a version bump from < 18.0.1.12.0
to >= 18.0.1.12.0.  Bumping past 18.0.1.12.0 in the future will
not re-execute this script.
"""


def migrate(cr, version):
    if not version:
        # Fresh install — no config to clear.
        return
    cr.execute(
        """
        DELETE FROM ir_config_parameter
         WHERE key IN (
            'orc.endpoint_url',
            'orc.rotation_days',
            'orc.org_token',
            'orc.infrastructure_id'
         )
        """
    )
