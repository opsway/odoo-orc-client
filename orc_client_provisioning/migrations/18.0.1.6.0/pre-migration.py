"""Drop the now-orphaned orc_access_level columns.

INT-842 dropped the per-user `read / write` API access axis on the
ORC side. The addon's matching fields (and the read-only enforcement
that read them) are gone. Drop the columns so the schema doesn't
carry dead state.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        ALTER TABLE res_users_apikeys
            DROP COLUMN IF EXISTS orc_access_level
        """
    )
    cr.execute(
        """
        ALTER TABLE res_users
            DROP COLUMN IF EXISTS orc_access_level
        """
    )
    _logger.info("[orc] dropped orc_access_level from res_users + res_users_apikeys")
