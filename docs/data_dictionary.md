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

## Database schema and ownership

The Alembic revision creates eleven application tables and the separate
`alembic_version` bookkeeping table. All application tables have an `id`
primary key (`INTEGER` on SQLite; `BIGINT` on PostgreSQL). Telegram identifiers
are signed 64-bit integers independent of internal IDs. Foreign keys have no
cascading deletes, so deleting a source with history fails.

| Table | Ownership and identity |
|---|---|
| `sources` | Unique stable Telegram chat ID; history and live checkpoints are separate. |
| `messages` | Owned by a source; unique Telegram message ID within that source. |
| `message_revisions` | Owned by a message; unique revision number within it. The indexed content hash can repeat. |
| `extraction_runs` | Owned by a revision; globally unique request key. A manual run has a parent from the same revision. |
| `job_postings` | Owned by a run; unique posting number within it. |
| `experience_requirements` | Owned by a posting; may also refer to a vocabulary keyword. |
| `salary_offers` | Owned by a posting; stores exact scaled integers and explicit units. |
| `keywords` | Unique canonical code for a vocabulary entry. |
| `keyword_mentions` | Owned by a posting and linked to a keyword; occurrences can repeat. |
| `review_issues` | Owned by a run; an optional posting must belong to it, and a resolution run must use the same revision. |
| `duplicate_links` | Ordered pair of revisions from different messages, with a unique pair and retained review decision. |

The `messages.current_revision_id` foreign key includes the message's own ID.
The `message_revisions.active_extraction_run_id` foreign key includes the revision's
ID and an internal `active_run_status` column constrained to `completed`. This
prevents selecting an unfinished run, even through direct SQL. SQLite declares
these two circular foreign keys during table creation; PostgreSQL adds them
after the referenced tables exist. `review_issues.message_revision_id` and the
two message IDs in `duplicate_links` are internal keys used to enforce ownership
with composite foreign keys.

The ORM accepts timezone-aware datetimes and normalizes them to UTC. SQLite
stores UTC with no timezone suffix and restores it as aware UTC on read.
PostgreSQL DDL uses `TIMESTAMP WITH TIME ZONE`; execution against a PostgreSQL
server has not been verified. Evidence and field states use JSON. Reporting
dimensions remain separate columns, with indexes for publication dates, run
status, content hashes, role family, keyword context, and review status.

SQLite connections enforce foreign keys, use a five-second busy timeout, and
enable WAL for file databases. The storage transaction boundary serializes
short-lived sessions within one process. The extraction writer validates
evidence, registers referenced vocabulary entries, and saves completed automatic
snapshots and active pointers atomically.

## Message history, checkpoints, and extraction requests

Source registration uses the stable Telegram chat ID. Re-registering the same
channel refreshes its display metadata without resetting its original backfill
window or checkpoints. Registering a different enabled channel requires an
explicit source change.

A replay with the same exact text, content kind, and Telegram edit timestamp
reuses the stored revision, even if that revision is no longer current. For a
new version, a strictly newer Telegram edit timestamp can replace the current
revision. The comparison uses the publication time as the baseline for the
initial version with no edit stamp. An incoming observation with no edit stamp,
an equal stamp, or an older stamp never replaces an existing selection. A
different observation is still retained with its original text and timestamps.
The observed time records when it arrived; it does not decide source order.
A newer edit with unchanged text, or a newer edit back to earlier text, is a
separate revision. The revision hash covers the exact UTF-8 text.

Each revision, including a retained historical version, gets one initial
automatic request in the same transaction. A replay or restart does not change
the request key or its pinned parser and taxonomy versions. Historical jobs do
not affect which revision is current. The parser and selection of completed
extractions are separate from this storage step.

History pages advance a descending cursor within the persisted backfill window.
Live callbacks can save observations but do not advance the live checkpoint.
Only a caller that has reconciled a bounded range supplies its covered upper
ID; sparse message IDs are allowed, and older observations can accompany the
range to reconcile edits. Each batch and checkpoint change commit together.
The write requires the stored checkpoint to match the caller's starting
checkpoint. A mismatch rolls back the batch; reread and retry from storage.

Claiming a request records the attempt before processing begins. Startup
recovery requeues interrupted claims with their existing attempt count, or
marks them failed once the limit is reached. Recovery is explicit and must run
only after the caller has exclusive workspace ownership and no worker can
still be running. The offline commands acquire a POSIX file lock beside the
resolved SQLite database before processing or enqueueing new requests.

## Local processing and result selection

The worker releases the claim transaction before parsing. It verifies the pinned
parser and taxonomy versions and rejects a stale attempt when saving or failing
a claim. A completed result includes its posting, experience, salary, keywords,
and review issues in one transaction. Referenced keyword codes are registered
idempotently; a conflicting stored category is rejected and historical entries
are retained. Errors store categories rather than raw exception or source text.

A completed result for a superseded revision stays in history without becoming
active. For the current revision, the highest completed automatic request ID
wins unless a manual result is selected. Pending and failed requests leave
the selection intact. A new message revision does not inherit an old selection.

The retry migration adds nullable `extraction_runs.retry_not_before`. A recognized
SQLite busy or locked error during result persistence can requeue the same
request with a delay (5 seconds after attempt one, 10 after attempt two by
default). The default attempt limit is three. Future retries survive a restart
and do not block other eligible requests. A failure that also prevents recording
its own status leaves the durable claim for startup recovery. Unavailable
versions, invalid results, unexpected parser errors, and other storage errors
do not retry automatically.

## Deterministic extraction

`job_market.parsing.pipeline.parse_vacancy(original, content_kind=...)` is a pure
function over one source post. Parser version `1.0.0` uses the packaged role,
keyword, and section vocabulary. It checks English, Ukrainian, and Russian cues
together and maps normalized matches back to exact original Unicode text.
Versioning the rules separately from the taxonomy preserves the identity of an
interpretation. An empty or unsupported post cannot produce a vacancy.

- Hiring statements and explicit titles take precedence over hashtags. A
  supported title with additional vacancy evidence can establish a posting.
  Isolated headings and conflicting titles can require review. Separately
  advertised roles yield `multiple_vacancies`; alternatives for one position
  can preserve the title without inventing one normalized role.
- Section and local wording determine keyword context. Separate occurrences can
  carry different contexts or qualification levels. Contact and offer text is
  excluded from skill extraction. Slash-separated skills do not imply that all
  or any one is required. Short aliases such as `Go` and `R` need local context.
- Experience retains the wording and scope of each requirement. A duration is
  recorded only when it can be converted exactly to whole months. A stated
  minimum is not a maximum; an explicit no-experience policy has no numeric
  duration. Malformed duration bounds can block the experience metric alone.
- Salary rules recognize explicit bounds, common numeric forms, currencies,
  periods, and tax cues. For example, `$2,500` has a numeric amount and an
  ambiguous currency code; its period stays unspecified unless stated.
  Matching offers merge their compatible evidence. Approximate, conflicting,
  malformed, or inexact amounts can block salary metrics while leaving the
  posting and compensation evidence available. No currency conversion occurs.
- Work mode, schedule, organizational arrangement, and compensation components
  are independent. In-house does not imply on-site, and a company team's setup
  does not establish the hire's arrangement. Fixed pay plus bonuses can be
  recorded even when there is no numeric offer.

These rules cover supported wording and taxonomy entries; they do not provide
general language understanding or exhaustively recognize every title inflection,
layout, or compensation expression. The three sanitized reference posts and
synthetic checks test specified behavior. They do not measure production accuracy.
