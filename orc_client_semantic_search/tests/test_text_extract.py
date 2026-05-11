"""Tests for ``utils.text_extract``.

Pure-stdlib helpers: no Odoo state needed. Inheriting from Odoo's
``BaseCase`` keeps the test discovery uniform with the rest of the
suite (Odoo's test runner picks them up via --test-tags).
"""
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.orc_client_semantic_search.utils.text_extract import (
    EXTRACTORS,
    html_strip,
    plain,
)


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class HtmlStripTests(TransactionCase):
    def test_collapses_visible_text(self):
        # The dominant input shape — knowledge.article body fragments.
        out = html_strip("<h1>Title</h1><p>Hello <b>world</b>.</p>")
        self.assertIn("Title", out)
        self.assertIn("Hello world.", out)
        self.assertNotIn("<h1>", out)
        self.assertNotIn("<b>", out)

    def test_decodes_html_entities(self):
        out = html_strip("<p>&amp; &lt;tag&gt; &nbsp;done</p>")
        self.assertIn("& <tag>", out)

    def test_block_tags_introduce_newlines(self):
        # Two paragraphs separated by visible whitespace so a
        # downstream embedding model still recovers paragraph
        # structure once tags are gone.
        out = html_strip("<p>first</p><p>second</p>")
        self.assertRegex(out, r"first\s*\n+\s*second")

    def test_list_items_render_as_bullets(self):
        out = html_strip("<ul><li>alpha</li><li>beta</li></ul>")
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        # Not pinning the bullet character — just that each item
        # gets its own line.
        alpha_line = next(line for line in out.splitlines() if "alpha" in line)
        beta_line = next(line for line in out.splitlines() if "beta" in line)
        self.assertNotEqual(alpha_line, beta_line)

    def test_falsy_inputs_return_empty(self):
        # Odoo's HTML fields default to False, not "". Both must
        # produce empty output without raising.
        self.assertEqual(html_strip(False), "")
        self.assertEqual(html_strip(""), "")
        self.assertEqual(html_strip(None), "")


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class PlainTextExtractorTests(TransactionCase):
    def test_passthrough(self):
        self.assertEqual(plain("hello"), "hello")

    def test_falsy_inputs_return_empty(self):
        self.assertEqual(plain(False), "")
        self.assertEqual(plain(None), "")


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class ExtractorRegistryTests(TransactionCase):
    def test_registry_keys_match_config_selection(self):
        # The keys here are pinned by the orc.embedding.config
        # ``text_extractor`` Selection field. If a key is added or
        # removed without updating the field's choices, the cron
        # will silently misroute records.
        self.assertEqual(
            sorted(EXTRACTORS.keys()),
            ["attachment", "html_strip", "plain"],
        )
