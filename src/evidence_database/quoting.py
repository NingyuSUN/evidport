"""Take a quotation from the stored text instead of having it typed.

Whoever types a quotation changes it: a word broken at a line end is joined, a
symbol the text layer stored as a control character is written out, two passages
are stitched with an ellipsis. ``check`` is exact on purpose, so such a quotation
is not found. Two ways around that, neither of which loosens ``check``:

``sentences`` / ``sentence_span``
    Number the sentences of a block. The caller names positions and gets the
    stored text.
``align``
    Find the place a typed quotation corresponds to and return the stored text
    there, with what differed. Letters and digits are never substituted.

Both return whitespace-collapsed stored text, which ``check`` matches exactly.
"""
from __future__ import annotations

import itertools
import re
import unicodedata

from .verification import _DASHES, _DEFECTS, _QUOTES

_SENTENCE_END = re.compile(r"(?<=[.!?]) (?=[A-Z(\[])")
_ELLIPSIS = r"(?:\.{3,}|…|\[\s*(?:\.{3,}|…)\s*\])"
_EDGE_ELLIPSIS = re.compile(rf"^\s*{_ELLIPSIS}\s*|\s*{_ELLIPSIS}\s*$")
_INNER_ELLIPSIS = re.compile(rf"\s*{_ELLIPSIS}\s*")
_UNREADABLE = re.compile("|".join(pattern.pattern for code, pattern in _DEFECTS
                                  if code in ("control_character", "unmapped_glyph")))
_INVISIBLE = dict(_DEFECTS)["invisible_character"]
_ANY_SYMBOL = "￿"  # stands in the folded text for a character the text layer could not store
_MIN_PART = 12


def _collapse(text: str) -> str:
    return " ".join(text.split())


def sentences(text: str) -> list[tuple[int, int]]:
    """Character ranges, in the whitespace-collapsed text, of its sentences.

    A sentence is a unit of selection, not of grammar: an abbreviation may end one
    early. Joining consecutive sentences with one space gives back the collapsed text.
    """
    collapsed = _collapse(text)
    cuts = [0] + [match.end() for match in _SENTENCE_END.finditer(collapsed)] + [len(collapsed) + 1]
    return [(start, end - 1) for start, end in itertools.pairwise(cuts) if end - 1 > start]


def sentence_span(text: str, first: int, last: int | None = None) -> str:
    """The stored text of sentences ``first`` to ``last`` (1-based, inclusive)."""
    spans = sentences(text)
    last = first if last is None else last
    if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(spans):
        raise ValueError(f"Sentence positions must be integers with 1 <= first <= last <= {len(spans)}")
    return _collapse(text)[spans[first - 1][0]:spans[last - 1][1]]


def _style(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_DASHES).translate(_QUOTES)


def _fold(collapsed: str):
    """The text as a reader would retype it, and where each folded character came from."""
    folded, origin, joined = [], [], set()
    index, size = 0, len(collapsed)
    while index < size:
        character = collapsed[index]
        if (character in "-­" and index and collapsed[index - 1].isalpha()
                and collapsed[index + 1:index + 2] == " " and collapsed[index + 2:index + 3].islower()):
            joined.add(index)  # a word broken at a line end
            index += 2
            continue
        if character == "­" or _INVISIBLE.fullmatch(character):
            index += 1
            continue
        for piece in (_ANY_SYMBOL if _UNREADABLE.fullmatch(character) else _style(character)):
            folded.append(piece)
            origin.append(index)
        index += 1
    return "".join(folded), origin, joined


def _candidates(collapsed: str, quote: str) -> list[dict]:
    wanted = _style(_collapse(quote))
    if not wanted:
        return []
    folded, origin, joined = _fold(collapsed)
    # A symbol the text layer lost may stand for any one character that is not a letter, digit or space.
    pattern = "".join(re.escape(c) if c.isalnum() or c == " " else f"(?:{re.escape(c)}|{_ANY_SYMBOL})" for c in wanted)
    found = []
    for match in re.finditer(f"(?=({pattern}))", folded):
        start, end = origin[match.start(1)], origin[match.end(1) - 1] + 1
        stored = collapsed[start:end]
        differences = []
        if any(start <= index < end for index in joined):
            differences.append("line_end_hyphenation")
        if _ANY_SYMBOL in match.group(1):
            differences.append("symbol_not_stored_by_text_layer")
        if _style(stored) != stored or _style(_collapse(quote)) != _collapse(quote):
            differences.append("dash_quote_or_ligature_style")
        if not any(row["start"] == start and row["end"] == end for row in found):
            found.append({"start": start, "end": end, "quote": stored, "differences": differences})
    return found


def align(text: str, quote: str) -> list[dict]:
    """Stored text that a typed quotation corresponds to.

    One row when the whole quotation corresponds to exactly one place; several, in
    reading order, when it was stitched from passages with an ellipsis and every part
    has exactly one place. Empty when nothing corresponds or the place is ambiguous.
    Each row is ``{"start", "end", "quote", "differences"}`` over the collapsed text.
    """
    collapsed = _collapse(text)
    trimmed = _EDGE_ELLIPSIS.sub("", _EDGE_ELLIPSIS.sub("", _collapse(quote)))
    whole = _candidates(collapsed, trimmed)
    if len(whole) == 1:
        if trimmed != _collapse(quote):
            whole[0]["differences"].append("ellipsis_removed")
        return whole
    parts = [part for part in _INNER_ELLIPSIS.split(trimmed) if part]
    if whole or len(parts) < 2 or any(len(part) < _MIN_PART for part in parts):
        return []
    rows = [_candidates(collapsed, part) for part in parts]
    if any(len(row) != 1 for row in rows):
        return []
    rows = [row[0] for row in rows]
    if any(a["end"] > b["start"] for a, b in itertools.pairwise(rows)):
        return []
    for row in rows:
        row["differences"].append("stitched_with_ellipsis")
    return rows
