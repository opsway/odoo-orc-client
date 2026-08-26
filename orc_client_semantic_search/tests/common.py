"""Shared base for tests that run the indexing sweep.

The sweep records what it spent on the daily token counter, and it does
that on ``pool.cursor()`` — deliberately, because a provider charge
cannot be rolled back, so neither may the record of it (see
``orc.embedding.config._token_budget_consume``).

That cursor is only a proxy onto the test transaction while the registry
is in *test mode*. ``HttpCase`` enters it; ``TransactionCase`` does not.
So a plain ``TransactionCase`` that sweeps gets a real second connection
which really commits, and the damage is not subtle:

- the charge outlives the test, so the next run starts against a
  poisoned counter and cap assertions fail for no visible reason;
- once it has committed, the test transaction's own write to that row
  fails with "could not serialize access due to concurrent update" —
  which takes out every later test in the class, including ones that
  have nothing to do with the token cap.

Sweeping tests therefore inherit from ``SweepCase`` rather than
``TransactionCase``. Anything that only reads, or that never reaches the
sweep, does not need it.
"""
from odoo.tests.common import TransactionCase


class SweepCase(TransactionCase):
    """``TransactionCase`` whose ``pool.cursor()`` stays inside the test
    transaction, so the sweep's token accounting rolls back with it."""

    def setUp(self):
        super().setUp()
        # Odoo renamed the entry point in 19.0 — `Registry.enter_test_mode`
        # is gone, replaced by `registry_enter_test_mode` on the test case
        # (which wraps `self.cr` and registers its own cleanup). Dispatch on
        # what the test case offers rather than on a version number, so this
        # file stays identical on the 18.0 and 19.0 branches; same shape as
        # `orc_client_provisioning.tests.common.share_test_cursor`.
        if hasattr(self, "registry_enter_test_mode"):  # 19.0+
            self.registry_enter_test_mode()
        else:  # 18.0
            self.registry.enter_test_mode(self.cr)
            self.addCleanup(self.registry.leave_test_mode)
