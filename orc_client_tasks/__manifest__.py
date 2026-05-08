{
    "name": "ORC Client — Tasks",
    "version": "18.0.0.2.0",
    "summary": (
        "In-Odoo chat dock for ORC: task-list systray + Discuss-style"
        " foldable windows embedding ORC chat via SSO."
    ),
    "description": """
ORC Client — Tasks (Phase 2a)
=============================

Depends on ``orc_client_provisioning``. Adds:

- A systray button listing the user's ORC tasks.
- A Discuss-lookalike dock at the bottom-right that holds foldable
  chat windows — each window is an iframe into
  ``/dashboard/tasks/{room_id}?embed=1`` on ORC, signed in via the
  one-time SSO nonce the addon mints server-to-server.
- Odoo-side proxy controllers that forward task-list and task-create
  calls to ORC with Bearer + X-Acting-User.

Not yet in this phase: exception-to-ticket, record-context task
creation, and the shared-SSE message body (iframe per window for
now; see docs/orc-client-tasks-roadmap.md in the parent repo).
""",
    "author": "OpsWay",
    "website": "https://opsway.com",
    "license": "LGPL-3",
    "category": "Productivity",
    "depends": ["orc_client_provisioning"],
    "external_dependencies": {"python": ["requests"]},
    "data": [],
    "assets": {
        "web.assets_backend": [
            "orc_client_tasks/static/src/scss/orc_chat.scss",
            "orc_client_tasks/static/src/js/orc_chat_service.js",
            "orc_client_tasks/static/src/js/orc_chat_window.js",
            "orc_client_tasks/static/src/js/orc_chat_dock.js",
            "orc_client_tasks/static/src/js/orc_task_list_popover.js",
            "orc_client_tasks/static/src/js/orc_systray_override.js",
            "orc_client_tasks/static/src/xml/orc_chat_templates.xml",
        ],
    },
    "installable": True,
    "application": False,
}
