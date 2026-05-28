-- Odoo Database Neutralization for the AI Workplace ("ORC") addon.
--
-- This file is discovered automatically by Odoo's neutralize routine
-- (`odoo.modules.neutralize`) whenever a DB is restored with the
-- "neutralize" flag — the default on Odoo.sh staging branches and
-- on Database Manager > Duplicate when the operator ticks the
-- "neutralize" checkbox.  Production restores leave it untouched.
--
-- What we sanitize:
--   The addon stores four `ir.config_parameter` rows that tie this
--   Odoo to a specific AI Workplace endpoint + a specific Bearer
--   token + a specific infrastructure row on the AI Workplace side:
--       orc.endpoint_url       (which AI Workplace this Odoo talks to)
--       orc.rotation_days      (key rotation cadence)
--       orc.org_token          (Bearer for addon → AI Workplace calls)
--       orc.infrastructure_id  (UUID of THIS Odoo on the AI Workplace side)
--
--   Carrying these forward into a duplicated / staging DB would let
--   the copy keep authenticating against the live AI Workplace and
--   make writes against the production org_api_token + infrastructure
--   rows — exactly the failure mode neutralize.sql exists to prevent.
--
-- After neutralize:
--   The addon's `orc.client` reads `orc.endpoint_url`; with the row
--   gone it raises a UserError prompting the operator to re-provision.
--   Provisioning against a NEW AI Workplace endpoint (e.g. a staging
--   ORC) writes fresh rows; the staging Odoo is then isolated from
--   production AI Workplace state.
--
-- Sister cleanups handled by neutralize:
--   - cron jobs are auto-disabled by Odoo's core neutralize step
--     (`UPDATE ir_cron SET active = FALSE`), so the key-rotation
--     cron won't fire against the dropped config.
--   - mail.mail outbound + payment.provider rows are sanitized by
--     `base` and `payment`'s own neutralize files.

DELETE FROM ir_config_parameter
 WHERE key IN (
    'orc.endpoint_url',
    'orc.rotation_days',
    'orc.org_token',
    'orc.infrastructure_id'
 );
