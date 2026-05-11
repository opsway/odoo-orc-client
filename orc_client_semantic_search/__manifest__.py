{
    "name": "AI Workplace — Semantic Search",
    "version": "18.0.0.2.1",
    "summary": "Permission-aware semantic search over Odoo records, callable by the AI Workplace agent.",
    "description": """
AI Workplace — Semantic Search
============================

Indexes Odoo records (knowledge.article in v1) with vector
embeddings and exposes a single XML-RPC method,
``orc.embedding.semantic_search(query, models?, limit?)``, returning
refs only — ``[{model, id, score}]``. Permissions stay where they
already work: the AI Workplace agent reads candidates as the end user, and
Odoo's ``ir.rule`` filters server-side. No parallel ACL system, no
gateway dependency.

See ``README.md`` for the full contract and ``AGENTS.md`` for
maintainer guidance.
""",
    "author": "OpsWay",
    "website": "https://opsway.com",
    "license": "LGPL-3",
    "category": "Productivity",
    "depends": ["base", "mail", "knowledge"],
    "external_dependencies": {"python": ["numpy", "requests"]},
    "data": [
        "security/orc_embedding_security.xml",
        "security/ir.model.access.csv",
        "data/orc_embedding_config_data.xml",
        "data/ir_cron.xml",
        "views/orc_embedding_views.xml",
    ],
    "installable": True,
    "application": False,
}
