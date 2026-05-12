# Tests are loaded by Odoo's test runner via --test-tags=orc_client_semantic_search.
# Each module here is imported on test discovery; keep this list ordered roughly
# by dependency (utilities first, then provider, then high-level lifecycle).
#
# v15 port note: the lifecycle tests that seed `document.page` fixtures
# (test_hash_skip, test_indexing_lifecycle, test_semantic_search) are
# deferred to a follow-up — they need real `document.page` records and
# document_page's history-driven content compute is non-trivial to fake.
from . import test_text_extract
from . import test_cosine
from . import test_provider_openai
from . import test_data_model
