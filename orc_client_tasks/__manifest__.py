{
    "name": "ORC Client — Tasks",
    "version": "18.0.0.1.0",
    "summary": "Rich in-Odoo ticket creation UX for ORC (Phase 2 stub).",
    "description": """
ORC Client — Tasks (Phase 2)
============================

Depends on ``orc_client_provisioning``. Adds:

- Systray dropdown with unread counts and a "Create Ticket" button
- Per-record "Create ORC Ticket" action-menu entry
- Exception-modal override: turn any user-facing error into a ticket
  with the traceback pre-attached (scrubbed of known secrets)
- AUP acceptance embedded in the Create dialog, persisted in both
  Odoo and ORC (compliance_acks)
- Local mirror of tasks (``orc.ticket``) with a 5-minute polling
  sync cron

NOT YET IMPLEMENTED. This manifest exists so Phase 2 can ship without
reinstalling Phase 1.
""",
    "author": "OpsWay",
    "website": "https://opsway.com",
    "license": "LGPL-3",
    "category": "Productivity",
    "depends": ["orc_client_provisioning"],
    "data": [],
    "installable": False,
    "application": False,
}
