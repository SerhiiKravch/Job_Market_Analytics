# Extraction contracts and taxonomy

The domain models accept data from ingestion and parsing and are independent of
Telethon and SQLAlchemy. A message observation carries the original text, content
kind, Telegram identifiers, and timezone-aware timestamps normalized to UTC.
Channel IDs are signed 64-bit integers; message IDs are positive. An empty or
unsupported observation can still be recorded.

An extraction result is a complete interpretation of one revision. It carries
parser and taxonomy versions, classification, source evidence, and any issues.
Only a `single_vacancy` result has a posting, numbered 1. A digest, candidate
post, other content, or uncertain result has no posting. An uncertain result
must include an issue that blocks the posting. Database identifiers, run status,
issue resolution, and history belong to the persistence layer.

Accepted posting facts need evidence. Quotes preserve source wording, and a role
name inferred through the taxonomy records the mapping code with its evidence.
Evidence offsets are optional; when present they are paired, zero-based Unicode
character indexes with an exclusive end. Quotes without offsets must still occur
in the source. Call `ExtractionResult.validate_source(full_text)` against the
exact revision before accepting or persisting a result.

## Uncertainty and units

- An absent value stays null or uses the dimension's explicit `unspecified`
  code. `unclassified` denotes an unknown role family. An unset compensation
  flag means the component was not explicitly detected, without asserting absence.
- A supplied field state is `stated`, `not_stated`, `ambiguous`, or
  `conflicting`. Only `stated` accompanies an accepted value. If the state is
  omitted, presence or absence of the accepted value conveys the default.
  Keep conflicting salary candidates out of numeric offers and attach a
  salary-blocking issue to `posting.salary_offers` when warranted.
- Experience retains scope, context, and the quoted requirement. Duration is
  stored as whole months. The exact unit conversion accepts years or months,
  rejecting fractions that cannot become a whole month. A requirement can have
  no duration. Explicitly no prior experience required is a policy, with no
  numeric duration.
- Salary is a nonnegative signed 64-bit integer in units of 0.0001 currency
  units (2,500 is 25,000,000). Use decimal input; floats, rounding, and overflow
  are rejected. A range has both ordered bounds; an exact offer has two equal
  bounds. One-sided offers populate only the stated bound. Currency codes must
  be three uppercase ASCII letters and require supporting evidence; a symbol
  like `$` alone does not identify the currency. Period, tax basis, and
  compensation component remain separate dimensions.
- Keyword mentions use a stable vocabulary code, original term, evidence, and
  context. Repeated occurrences can have different contexts and qualification
  levels. The domain does not infer the relationship between slash-separated
  alternatives.

## Review paths

Issue drafts identify `classification`, `posting`, a named posting field
(e.g. `posting.role_normalized`), or a child collection:
`posting.experience_requirements`, `posting.salary_offers`, or
`posting.keyword_mentions`. A collection can target an existing zero-based
index and optionally a factual field, e.g. `posting.salary_offers[0].tax_basis`.
These paths describe issues; they do not grant write access to arbitrary
attributes. A metric-blocking issue must name a controlled metric code.

## Versioning and development fixtures

`load_taxonomy()` reads `roles.json`, `keywords.json`, and `sections.json` from
the installed package. All three must carry the same version. Version 1.0.0
covers all classified role families and all keyword categories, with initial
English, Ukrainian, and Russian aliases and headings. Contextual aliases (such
as `UA Manager`, `Go`, and `R`) specify supporting terms; the matching logic is
a separate step. A substantive identity or category change requires a new
canonical code rather than reusing a historical identity. Revise the common
resource version when changing the vocabulary or its cues.

The three UTF-8 posts in `tests/fixtures/posts/` retain the sanitized source
examples. JSON annotations in `tests/fixtures/expected/` use the extraction
contract and the marker `reference-1` to distinguish them from parser output.
They cover selected evidenced interpretations and negative expectations for
development, with no claim of held-out coverage or measured accuracy.
