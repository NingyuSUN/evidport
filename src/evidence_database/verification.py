"""Machine checks that can settle a quotation's transcription without a person.

A PDF text layer and an OCR reading of the rendered page region fail in different
ways. When both give the same quotation, and the quoted characters carry none of
the known text-layer defects, the transcription is treated as machine-verified.
Anything else stays with a person. This never records a human review and says
nothing about context, entailment or scientific support.

Measured on 11 papers (3,036 paragraph blocks, 2026-11-17): the text layer was
wrong in about 0.5% of words, concentrated in the defect classes detected here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import unicodedata
from concurrent.futures import ThreadPoolExecutor

READING_DPI = 300
READING_PAD = 2.0  # PDF points around the block box, so ascenders and descenders are not clipped
READING_ARGUMENTS = ["stdin", "stdout", "--psm", "6", "-l", "eng", "--dpi", str(READING_DPI)]
OCR_COMMAND_ENV = "EVIDPORT_OCR_COMMAND"

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212\ufe63\uff0d"), "-")
_QUOTES = {ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201c"): '"', ord("\u201d"): '"', ord("\u00b4"): "'", ord("`"): "'",
           ord("\u2032"): "'", ord("\u2033"): '"'}

# Characters a text layer should never contribute to a quotation. Each class was
# observed replacing or hiding printed characters in real publisher PDFs.
# Two more classes come from typography recorded at conversion, not from characters:
#   superscript_position_lost  an exponent stored as 10-8, a citation mark glued to a word
#   symbol_font_letter         a letter set in a font that never spells a word. Such fonts map
#                              other glyphs onto letters (alpha extracted as a, micro as m), and
#                              an English OCR model reads the look-alike glyph the same wrong way.
_DEFECTS = (
    # A minus sign stored as U+0005 turned -33.33 into 33.33.
    ("control_character", re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")),
    ("invisible_character", re.compile(r"[\u200b-\u200f\u2060-\u2064\ufeff]")),
    # 'ti' ligatures stored as U+019F; private-use and replacement characters.
    ("unmapped_glyph", re.compile(r"[\ue000-\uf8ff\ufffd\u0180-\u01cc\u01dd-\u0217\u021c-\u024f]")),
    # 'Rödel' stored as 'R ¨odel'. A prime after a digit (5´ end) is left alone.
    ("detached_diacritic", re.compile(r"(?<![0-9])\s?[\u00a8\u02c6\u02dc\u00b8\u02da\u00b4`](?=[A-Za-z])"
                                      r"|\s[\u0300-\u036f]")),
    # 'adultbrainsamples,which': spaces lost between words.
    ("missing_word_space", re.compile(r"[A-Za-z]{3,},[A-Za-z]{3,}|(?<![A-Za-z])[A-Za-z]{28,}(?![A-Za-z])")),
)


def lenient(text: str) -> str:
    """Fold what a reader calls the same: ligatures, dash and quote styles, line-end hyphenation."""
    text = unicodedata.normalize("NFKC", text).translate(_DASHES).translate(_QUOTES)
    text = " ".join(text.split())
    return re.sub(r"(?<=[A-Za-z])[-\u00ad] (?=[a-z])", "", text).replace("\u00ad", "")


def text_defects(text: str) -> list[tuple[int, int, str]]:
    """Character ranges of ``text`` that carry a known text-layer defect."""
    return sorted((match.start(), match.end(), code) for code, pattern in _DEFECTS
                  for match in pattern.finditer(text))


def collapsed_positions(text: str) -> list[int]:
    """Index in ``' '.join(text.split())`` of every index of ``text``, plus the end."""
    positions, count, pending, started = [], 0, False, False
    for character in text:
        if character.isspace():
            positions.append(count)
            pending = started
        else:
            if pending:
                count += 1
                pending = False
            positions.append(count)
            count += 1
            started = True
    positions.append(count)
    return positions


def machine_verification(block: dict, quote: str, starts: list[int], matches) -> dict:
    """Decide whether two independent readings settle this exact quotation.

    ``starts`` are the valid match offsets of ``quote`` in the whitespace-collapsed
    block text; ``matches`` is the store's boundary-aware quotation test.
    """
    reading = block.get("independent_reading")
    engine = {key: reading.get(key) for key in ("engine", "engine_version")} if isinstance(reading, dict) else {}
    blocking = []
    if block.get("kind") != "paragraph":
        blocking.append("not_a_text_paragraph")
    if block.get("issues"):
        blocking.append("block_has_extraction_issues")
    if block.get("location_accuracy") != "region":
        blocking.append("no_exact_page_region")
    if blocking:
        return {"status": "not_eligible", "blocking": blocking, **engine}
    if not isinstance(reading, dict) or not isinstance(reading.get("text"), str):
        return {"status": "not_run", "blocking": ["independent_reading_not_recorded"], **engine}
    text = block["text"]
    collapsed = " ".join(text.split())
    flagged = text_defects(collapsed)
    try:
        positions = collapsed_positions(text)
        for field, code in (("superscripts", "superscript_position_lost"), ("symbol_font_spans", "symbol_font_letter")):
            for start, end in block[field]:
                if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
                    raise ValueError("typography range outside block text")
                flagged.append((positions[start], positions[end - 1] + 1, code))
    except (KeyError, TypeError, ValueError):
        # Absent or malformed typography: superscripts and symbol fonts cannot be told apart from
        # ordinary text, so nothing in this block is settled by machine.
        flagged.append((0, len(collapsed), "typography_not_recorded"))
    length = len(" ".join(quote.split()))
    reasons = None
    for start in starts:
        overlapping = sorted({code for low, high, code in flagged if low < start + length and high > start})
        if not overlapping:
            reasons = []
            break
        reasons = overlapping if reasons is None else reasons
    if reasons is None or reasons:
        return {"status": "not_eligible", "blocking": reasons or ["quote_not_found"], **engine}
    if matches(lenient(reading["text"]), lenient(quote)):
        return {"status": "agrees", "blocking": [], **engine}
    return {"status": "disagrees", "blocking": ["independent_reading_differs"], **engine}


def marked_ranges(text: str, lines: list[list[tuple[str, bool]]]) -> list[list[int]] | None:
    """Offsets in ``text.strip()`` of the marked spans, or None when the spans do not reproduce ``text``.

    ``lines`` holds, per printed line, the (span text, is marked) pairs of the same block; a mark
    means a superscript, or a letter set in a symbol font.
    The spans must give the same characters in the same order; only whitespace may differ,
    because span data can carry empty lines that the block text omits.
    """
    lead, cursor, ranges = len(text) - len(text.lstrip()), 0, []
    for line in lines:
        for span, raised in line:
            first = last = None
            for character in span:
                if character.isspace():
                    continue
                while cursor < len(text) and text[cursor].isspace():
                    cursor += 1
                if cursor == len(text) or text[cursor] != character:
                    return None
                first, last = cursor if first is None else first, cursor
                cursor += 1
            if raised and first is not None:
                start, end = first - lead, last + 1 - lead
                # Marked spans separated only by whitespace are one range.
                if ranges and not text[ranges[-1][1] + lead:first].strip():
                    ranges[-1][1] = end
                else:
                    ranges.append([start, end])
    return ranges if not text[cursor:].strip() else None


class RegionReader:
    """An OCR engine run as a command: PNG on standard input, text on standard output.

    The command is the Tesseract executable and any prefix needed to reach it, for
    example ``["tesseract"]`` or ``["wsl.exe", "-d", "Ubuntu", "--", "tesseract"]``.
    """

    def __init__(self, command, *, workers: int | None = None, timeout: float = 300):
        if (not isinstance(command, (list, tuple)) or not command
                or any(not isinstance(part, str) or not part for part in command)):
            raise ValueError("OCR command must be a nonempty list of strings")
        self.command = list(command)
        self.workers = workers or max(1, min(8, os.cpu_count() or 1))
        self.timeout = timeout

    @classmethod
    def from_setting(cls, command=None):
        """An explicit command, else the JSON list in EVIDPORT_OCR_COMMAND, else no reader."""
        if command is None and os.environ.get(OCR_COMMAND_ENV):
            try:
                command = json.loads(os.environ[OCR_COMMAND_ENV])
            except json.JSONDecodeError as exc:
                raise ValueError(f"{OCR_COMMAND_ENV} must be a JSON list of strings") from exc
        return cls(command) if command is not None else None

    def _run(self, arguments, data=None) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.command + arguments, input=data, capture_output=True, timeout=self.timeout, check=False,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"}, stdin=subprocess.DEVNULL if data is None else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def describe(self) -> dict:
        process = self._run(["--version"])
        banner = (process.stdout or process.stderr).decode("utf-8", "replace").strip().splitlines()
        if process.returncode or not banner:
            raise ValueError("OCR command did not report a version")
        name, _, version = banner[0].partition(" ")
        return {"engine": name, "engine_version": version or "unknown", "method": "ocr_of_rendered_region",
                "dpi": READING_DPI, "pad_points": READING_PAD, "arguments": READING_ARGUMENTS}

    def read(self, png: bytes) -> str:
        process = self._run(READING_ARGUMENTS, png)
        if process.returncode:
            raise ValueError(f"OCR command failed with exit code {process.returncode}")
        return process.stdout.decode("utf-8", "replace")

    def read_all(self, images: list[bytes]) -> list[str | None]:
        """Read every image; a failed reading is None, never an invented text."""
        def one(image):
            try:
                return self.read(image)
            except (OSError, ValueError, subprocess.SubprocessError):
                return None
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(one, images))
