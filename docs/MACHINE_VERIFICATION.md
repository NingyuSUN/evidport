# Machine verification of quotations

A person reviewing a quotation against the original page is checking one thing: that the
extracted characters are the printed ones. EvidPort can settle most of those checks by itself
and leave a person only the quotations it cannot settle.

## How it works

The PDF text layer and an OCR reading of the rendered page fail in different ways. During
conversion EvidPort reads every paragraph twice:

1. the text layer (PyMuPDF), which is what search and quotation matching use;
2. an OCR reading of the same page region, rendered at 300 dpi and passed to Tesseract.

`check` then treats an exact quotation as machine-verified when all of these hold:

- the quotation is in the block text, with the usual word and number boundaries;
- the same quotation is in the OCR reading, after folding only ligatures, dash and quote
  styles and line-end hyphenation; digits and letters are never folded;
- the quoted characters touch no known text-layer defect (below), no superscript and no
  letter set in a symbol font;
- the block is a located text paragraph with no extraction issue;
- no review of that quotation has been recorded. A recorded rejection or revocation always wins.

Everything else stays `pending_review`, with the reason in `machine_verification.blocking`.

| `blocking` code | Meaning |
|---|---|
| `independent_reading_differs` | The two readings disagree on this quotation |
| `superscript_position_lost` | The quotation includes a superscript: an exponent such as 10⁻⁸ is stored as `10-8`, a citation mark is glued to the previous word |
| `symbol_font_letter` | The quotation includes a letter set in a font that never spells a word in the document. Publisher symbol fonts map other glyphs onto letters: α is extracted as `a`, µ as `m` (so 500 µl reads as 500 ml). An English OCR model reads the look-alike glyph the same wrong way, so agreement cannot be trusted there |
| `control_character` | A symbol is stored as a control character; seen replacing minus signs |
| `unmapped_glyph` | A ligature or symbol is stored as an unrelated letter |
| `detached_diacritic` | An accent is stored apart from its letter |
| `invisible_character` | Zero-width characters inside the text |
| `missing_word_space` | Words are stored without the spaces between them |
| `typography_not_recorded` | Superscripts and fonts could not be located in this block |
| `independent_reading_not_recorded` | The conversion has no OCR reading of this block |
| `not_a_text_paragraph`, `block_has_extraction_issues`, `no_exact_page_region` | Tables, rotated pages and page-only locations are not machine-verified |

## Turn it on

Add a `verification` object to the project configuration:

```json
"verification": {
  "admission": "independent_reading",
  "ocr_command": ["tesseract"]
}
```

`ocr_command` is the Tesseract executable with any prefix needed to reach it. The page region
is sent as a PNG on standard input and the reading is taken from standard output, so the
engine can run elsewhere, for example `["ssh", "ocr-host", "tesseract"]` or
`["wsl.exe", "--", "tesseract"]`. Only rendered regions of your papers are sent to that command.

- `ocr_command` alone records readings and reports `machine_verification` in every check, while
  admission still needs a human review.
- `admission: "independent_reading"` lets agreement admit a quotation. The default,
  `human_review`, keeps the earlier behaviour exactly.

Papers converted before a command was configured are extracted again as a new version on the
next `evidport project sync` or `evidport_papers_convert`. The earlier version and its reviews are kept.
Outside a project, set `EVIDPORT_OCR_COMMAND` to a JSON list for `evidport convert`, and pass
`--accept-independent-reading` to `evidport check`.

## What a result says

```json
{"admitted": true, "reason": "independent_reading_agreement", "admission_route": "independent_reading",
 "machine_verification": {"status": "agrees", "blocking": [], "engine": "tesseract", "engine_version": "5.5.0"},
 "review_id": null, "scientifically_verified": false}
```

`admission_route` is `human_review` for a recorded review and `independent_reading` for
agreement. Machine verification never writes to the review ledger and is never reported as a
human review. It settles transcription only: context, entailment and scientific support are
separate checks, and a consumer that requires a recorded human review must keep requiring it.

## How far agreement can be trusted

Agreement is not proof: two readings can be wrong in the same way. This was tested on 12
papers (2026-11-17). All 3,144 sentences admitted by agreement were read a third time with an
unrelated engine (RapidOCR), line by line:

- 2,277 sentences: all three readings gave the same characters;
- 457: the third engine did not read every line, so it added no information;
- 410: the third engine read something else. In 53 only punctuation or symbols differed
  (not examined further) and in 180 only look-alike glyphs such as `o`/`0` or `I`/`l`. The
  other 177 places and 23 of the look-alike ones were compared with the rendered page.

Of the 198 places that could be located on the page, the text layer was right in 197. The one
error was a Greek α extracted as `a` from a symbol font and read as `a` by Tesseract as well.
The symbol-font rule above was added for it; a font scan of the same papers then found the
same class elsewhere (α, β, µ, ®, small capitals). With the rule, both known α errors are
held back, at the cost of 14 further sentences whose italic variables use a font of their own.
The page comparison was made by an AI assistant, not by a human reviewer.

## Limits

- Shared errors remain possible in classes not yet seen. The check above covers 12 papers
  from a few publishers; 157 look-alike differences and the punctuation-only ones were not
  compared with the page.
- OCR misreads superscripts and some symbols, so those quotations go to a person even when the
  text layer is right.
- Scanned pages without a text layer are not read by the baseline conversion.
- Pages are read with the English Tesseract model.
