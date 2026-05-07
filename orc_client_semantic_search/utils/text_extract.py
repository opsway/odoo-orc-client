"""Text extraction helpers used by the indexing cron.

Each indexed model picks an extractor by name on its
``orc.embedding.config`` row (``html_strip`` / ``plain`` /
``attachment``). Adding a new extractor: add a function here, expose
its key in ``EXTRACTORS``, document under README ``Configuration UI
→ Indexed models``.

All extractors take a single ``raw`` argument (the value pulled
from the record's ``text_field_path``) and return a ``str`` —
empty string if the input is missing or unsupported. Provider-side
token-limit handling lives in the cron worker, not here.
"""
from __future__ import annotations

import base64
import io
import re
from html.parser import HTMLParser


# Tags that should produce a newline before/after their content so
# paragraph structure survives the strip. Inline tags (``<b>``, ``<i>``,
# ``<a>``) deliberately don't appear here so wrapped text stays on one
# line.
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "table", "tr", "thead", "tbody", "tfoot",
    "blockquote", "pre",
}


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        # convert_charrefs=True lets us handle entities (``&amp;``,
        # ``&nbsp;``) for free instead of writing a second pass.
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")
        elif tag == "li":
            self._parts.append("\n- ")
        elif tag == "br":
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        # Collapse runs of 3+ newlines to 2 (paragraph gap).
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        # Collapse internal runs of spaces/tabs without touching
        # newlines.
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = "\n".join(line.rstrip() for line in joined.splitlines())
        return joined.strip()


def html_strip(raw) -> str:
    """Strip HTML to plain text. Decodes entities, inserts newlines
    around block elements so paragraph structure survives, renders
    ``<li>`` as ``- `` bullets. Pure stdlib."""
    if not raw or not isinstance(raw, str):
        return ""
    parser = _Stripper()
    parser.feed(raw)
    parser.close()
    return parser.text()


def plain(raw) -> str:
    """Pass through, coercing to ``str`` and treating ``None``/``False``
    as empty."""
    if not raw:
        return ""
    return str(raw)


def attachment(raw) -> str:
    """Decode an ``ir.attachment``'s ``datas`` field (base64 bytes)
    and dispatch on mimetype.

    For v1 we accept either a base64-encoded string (Odoo's normal
    storage) or already-decoded bytes (e.g. when called from a test).
    The mimetype is sniffed from the first few bytes; PDFs go
    through ``pypdf``, text-shaped MIME types decode as utf-8."""
    if not raw:
        return ""

    data: bytes
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    elif isinstance(raw, str):
        try:
            data = base64.b64decode(raw)
        except Exception:
            return ""
    else:
        return ""

    if not data:
        return ""

    # Sniff: PDF magic bytes vs text.
    if data[:5] == b"%PDF-":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = []
            for page in reader.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    pages.append(page_text)
            return "\n\n".join(pages)
        except ImportError:
            return ""
        except Exception:
            return ""

    # Try text decode. If utf-8 fails, fall back to latin-1 — better
    # to ship something than nothing.
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return data.decode("latin-1", errors="replace")


# Registry of extractors. The cron worker resolves by name from
# this map; tests pin the keys.
EXTRACTORS = {
    "html_strip": html_strip,
    "plain": plain,
    "attachment": attachment,
}
