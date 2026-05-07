# Tests are loaded by Odoo's test runner via --test-tags=orc_client_semantic_search.
# Each module here is imported on test discovery; keep this list ordered roughly
# by dependency (utilities first, then provider, then high-level lifecycle).
from . import test_text_extract
from . import test_cosine
from . import test_provider_openai
from . import test_data_model
from . import test_hash_skip
from . import test_indexing_lifecycle
from . import test_semantic_search
