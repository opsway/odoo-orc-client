-- Neutralization for the AI Workplace build reporter / self-enrollment addon.
--
-- Discovered automatically by `odoo.modules.neutralize` whenever a database is
-- restored with the neutralize flag — the default on Odoo.sh staging branches,
-- and on Database Manager > Duplicate when the operator ticks the box. It is
-- NOT listed in the manifest's `data`; Odoo finds it by path.
--
-- ODOO 15 NEVER RUNS THIS FILE. `odoo.modules.neutralize` arrived in 16.0, and
-- 15.0 has no neutralization machinery at all — not even in `base`. This file
-- is kept because it is the correct thing to run and because the addon may be
-- carried to a later version, but on v15 nothing below happens. Its job is
-- done instead by `sanitize_if_rebuilt` in `models/enrollment.py`, which keys
-- off build identity (`orc.bound_build`) rather than a neutralize flag,
-- because v15 offers no flag to read. Keep the two lists in step: anything
-- added here belongs in `_STALE_ON_REBUILD` there too.
--
-- Four keys, and each matters for a different reason.
--
-- `orc.enroll_secret` is a LIVE PREIMAGE. Anyone holding it can prove
-- ownership of the build that published its hash, so carrying one forward into
-- a duplicated or staging database means shipping a working proof into a copy
-- that has no business using it. This is the same class of failure the sister
-- file in `orc_client_provisioning` exists to prevent for `orc.org_token` —
-- and enrollment made it sharper, because a proof is exactly what the restored
-- copy would need to obtain a credential of its own.
--
-- `enroll_done_key` is the debounce, `<branch_slug>:<build_id>`. A stale one
-- restored from a dump would match nothing on a fresh build (new build id) and
-- so is mostly harmless — but a dump restored ONTO THE SAME BUILD would carry
-- a key that suppresses the first enrollment, which is exactly the moment the
-- build needs one. Clearing it costs nothing; leaving it risks a silent
-- non-reconnection, which is the failure this whole feature exists to end.
-- `enroll_base` is the third, and it is the one with teeth. It decides WHERE
-- a build submits its proof, and `orc.endpoint_url` is derived from it — so a
-- development override restored from a dump would send a real build's proof,
-- and all of its later API traffic, to whatever host that override names. The
-- in-source constant is the value that should win on a restored copy.
-- `orc.bound_build` is the build these credentials were issued to. It is only
-- read on v15, but it must not ride a dump either: a stamp naming the source
-- build is what the v15 sanitizer treats as proof the parameters are somebody
-- else's, and carrying a stale one into a database whose credentials were
-- legitimately re-issued would make it sanitize a build that is fine.
DELETE FROM ir_config_parameter
 WHERE key IN (
    'orc.enroll_secret',
    'orc_client_build_reporter.enroll_done_key',
    'orc_client_build_reporter.enroll_base',
    'orc.bound_build'
 );
