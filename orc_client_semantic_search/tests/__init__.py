# Tests are loaded by Odoo's test runner via --test-tags=orc_client_semantic_search.
# Each module here is imported on test discovery; keep this list ordered roughly
# by dependency (utilities first, then provider, then high-level lifecycle).
#
# v15 port note: the fixture-driven suites below (hash_skip, indexing_lifecycle,
# semantic_search, non_admin_access, index_scope, token_cap) were deferred by the
# first v15 port because `document.page`'s history-driven `content` compute looked
# hard to fake. It is not: `create({"name": ..., "content": ...})` runs the
# inverse and lands a `document.page.history` row, so the fixtures work as
# written. They are all live again here.
from . import test_text_extract
from . import test_cosine
from . import test_provider_openai
from . import test_data_model
from . import test_hash_skip
from . import test_indexing_lifecycle
from . import test_semantic_search
from . import test_non_admin_access
from . import test_index_scope
from . import test_token_cap
