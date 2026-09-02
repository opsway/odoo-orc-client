"""Rename existing auto-managed API keys to the new AI Workplace label.

The addon previously created keys named "ORC (auto-managed)". The
constant is renamed to "AI Workplace (auto-managed)" — without this
migration, all existing rows on res.users.apikeys would orphan from
the addon's lookup (search by name=ORC_KEY_NAME).
"""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE res_users_apikeys
        SET name = 'AI Workplace (auto-managed)'
        WHERE name = 'ORC (auto-managed)'
        """
    )
