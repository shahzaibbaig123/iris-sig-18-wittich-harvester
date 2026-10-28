"""Text normalization shared by parsers, filters, geo matching and hashing."""

from __future__ import annotations

import html
import re
import unicodedata

# Zero-width space/joiners, word joiner, BOM and soft hyphen. Seen in real
# headlines (e.g. "​​…Information zur Einwohnerversammlung").
_INVISIBLE = re.compile("[​‌‍⁠﻿­]")
_SPACES = re.compile(r"\s+")


def clean(value: str | None) -> str:
    """Unescape entities, drop invisible characters, NFC, collapse whitespace."""
    if not value:
        return ""
    value = html.unescape(value)
    value = _INVISIBLE.sub("", value)
    value = unicodedata.normalize("NFC", value).replace("\xa0", " ")
    return _SPACES.sub(" ", value).strip()


def fold(value: str | None) -> str:
    """Comparison key: clean, casefold and spell out umlauts/ß."""
    value = clean(value).casefold()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(src, dst)
    return value
