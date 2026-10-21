# Recipe: how the curator is built

The implementation of [architecture.md](architecture.md). For users: [guide.md](guide.md).

## 1. Technical choices

- Python ≥ 3.12, one package (`ssot-curator`, import `curator`, command `curator`), installed with
  `uv tool install` and never copied into instances.
- SQLite for the store of facts and the search index (FTS5); JSON Schema for configuration and LLM
  outputs; git (command line) for the instance repository.
- Every hard-coded text is in **English** (code, messages, prompts, templates, SSOT structure). The
  language of the content is `instance.language`.
- **Nothing type-specific in the code:** concept types, fields, folders, identity rules and domain
  words live in the type configuration (`curator/data/types/*.yaml`).
- Language data that the code needs (month names, relative dates, phrases that open a quoted reply,
  disclaimers, omitted-media markers) are tables in the modules, in Italian and English, to be extended
  for other languages.

## 2. Package layout

```
curator/
├── cli.py            # commands (click); --json on every command
├── pipeline.py       # the cycle: run, rebuild, decide, forget, entity proposals, render; staging and publish
├── config.py         # schema and parsing of curator.yaml; source rules, by-extension fallback
├── instance.py       # paths of an instance, single-writer lock
├── scanner.py        # scan of raw/, manifest (present, missing, excluded)
├── parts.py          # documents inside a file: messages of a mailbox, attachments
├── extractors/       # text, pdf, docx, table, image, email, whatsapp → sections with anchors
├── ocr.py            # Tesseract (TSV): words, boxes, confidence; images.reader
├── curated.py        # curated/ files (frontmatter + anchors)
├── chunker.py        # chunks within sections, rows per chunk for tables
├── quarantine.py     # fixed rules for content that looks like instructions
├── interpret.py      # interpretation request (prompt, schema, known keys)
├── accept.py         # facts of an answer: allowed types and fields, key → concept path, grounding
├── grounding.py      # quote verification
├── temporal.py       # dates in text, source date, relative dates
├── entities.py       # concept paths from keys, aliases, related concepts
├── reconcile.py      # reconciliation request and checks; duplicates found by code
├── quotecheck.py     # optional second check of quotes by the LLM (sampled)
├── derive.py         # states of facts and concepts from facts + judgments + decisions + rules
├── decisions.py      # decisions.jsonl with anchors
├── proposals.py      # entity and revision proposals
├── forget.py         # deletion rules (hashes), applied at every run and before the LLM
├── render.py         # OKF bundle, indexes, conflicts.md, log.md
├── validator.py      # validity checks before publishing
├── search.py         # FTS5 index and search
├── review.py         # review queue
├── status.py, doctor.py, schedule.py, backup.py, migrate.py, init_cmd.py, evaluate.py
├── demo.py           # `curator demo`: instance from the synthetic examples, fake backend
├── typecheck.py      # loading (installed or own file) and checking of types: errors explained, warnings
├── demo_files.py     # the synthetic examples (raw/ files), generated deterministically
├── llm/              # base, claude_code, fake, cache
└── data/             # installed with the package
    ├── types/        # type configurations (status: tested | draft)
    ├── prompts/      # versioned prompts (interpret-vN, reconcile-vN, entities-vN, quote-check-vN)
    ├── templates/    # ssot/CLAUDE.md, Claude Code settings and the ssot skill
    └── demo/llm/     # stored answers of the fake backend for the synthetic examples
examples/expected/    # the SSOT the synthetic examples give (compared byte by byte by the tests)
eval/                 # synthetic evaluation set of the template (format in eval/README.md)
tools/                # prepare_raw.py (redacted test data), pdfwrite.py, check_terms.py, check_secrets.py
tests/
```

## 3. Commands

| Command | What it does |
|---|---|
| `init --type T\|FILE.yaml [--language L] --llm B <folder>` | creates an instance: configuration copied from an installed type or from the owner's type file, checked first (with `curator_version`), folders, `.gitignore`, `.claude/` files, empty `ssot/`, git repository |
| `--version` · `types [--show T] [--check T\|FILE.yaml]` | version of the installed curator · the installed types with status and purpose; `--show` prints a type's YAML, `--check` lists every error (explained) and warning of a type |
| `demo <folder>` | creates a `plant-support` instance from the synthetic examples with the `fake` backend and its stored answers (copied into the instance, `demo-llm/`), runs it and prints the report, the status and the pages to look at |
| `doctor [--llm-check]` | configuration, version of the instance, folders (recreates the missing ones), git, backend (flags, `ANTHROPIC_API_KEY`), images and Tesseract, schedule, backup age and restore test; `--llm-check` makes a tiny real call |
| `migrate` | brings an older instance up to date: `curator.yaml` (text edits, comments kept), store of facts, missing `.claude/` files |
| `run [--dry-run] [--max-tokens N] [--only GLOB]… [--fail-on-pending]` | the cycle; `--dry-run` estimates and writes nothing; `--only` limits the documents processed |
| `rebuild [--no-cache] [--max-tokens N]` | reprocesses everything from `raw/` in a fresh store, re-applies the decisions; refuses to start with missing sources |
| `status` | last run, sources, facts, concepts, quarantine, missing sources, schedule, new concepts since the last entity proposals, sources to check by hand, conflicts, decisions not applicable |
| `validate` · `render` | validity checks only · regenerate the OKF files from the store |
| `search "<text>" [--type T]` | full-text search over `ssot/` and `curated/` (read-only) |
| `review [--type T] [--low-confidence]` | facts to verify, high-priority types and low confidence first |
| `approve <fact or concept>…` · `reject <fact>…` | decisions on facts |
| `conflicts` · `resolve <id> --keep A\|B\|both` | conflicts |
| `quarantine` · `release <id> --as-data --note` · `discard <id>` | quarantined sources |
| `entities [--propose] [--type T]` | merge/link proposals (with `--propose`: one LLM call per type) |
| `merge <a> <b>` · `link` · `separate` · `rename <uid> <path>` · `alias <uid> "<name>"` | concepts |
| `refile <fact>… --to <concept>` | move facts filed under the wrong concept |
| `revisions` · `supersede-source <old> <new>` | documents that look like versions of each other |
| `forget --source <id\|raw/path> \| --term "<text>" [--mode redact\|exclude] [--yes]` | deletion (privacy, legal) |
| `schedule` | prints the command for cron, Windows Task Scheduler or a Claude Code desktop task |
| `backup [--verify [zip]]` · `restore <zip> <new folder>` | backup of the instance and restore test |
| `eval [--save-baseline] [--baseline NAME] [--prompt P] [--source GLOB]… [--case ID]… [--max-tokens N]` | evaluation against a baseline |

Every command finds the instance from `--instance` or the current folder, and supports `--json`.

## 4. Configuration (`curator.yaml`)

```yaml
schema_version: 1
curator_version: "0.4.1"          # written by init, updated by migrate
instance: {name: my-ssot, type: client-project, language: en, owner: owner}
llm: {backend: claude-code, model: "", timeout_seconds: 300, max_tokens_per_run: 400000, cache: true}
chunking: {max_tokens: 3000, overlap_tokens: 150, table_rows_per_chunk: 60}
source_date: [content, filename, mtime]
ignore: ["**/~$*", "**/*.zip"]     # never read; a processed file added here is withdrawn
sources:                            # first match wins; a file outside every rule is read by extension
  # a folder says HOW its files are read (hint, allowed types), not their format: `auto` picks the extractor
  # by extension (a .txt that is a WhatsApp export is recognized); `hint` goes into every chunk header
  - {match: "notes/**/*.{txt,md}", extractor: note, hint: "Notes of the owner: first-hand knowledge."}
  - {match: "interviews/**/*", extractor: auto, hint: "Attribute every statement to the person who said it."}
  - {match: "manuals/**/*.pdf", extractor: pdf, summarize: false, extract: [Procedure], pages: {"big-manual*.pdf": "5-9"}}
concepts:
  Issue:
    folder: issues
    key: "{project}/{symptom}"      # variables come from the LLM (concept_key)
    title: "{symptom} ({project})"
    summary: "…"                    # one line for people, shown in the index pages (≤ 200 characters)
    description: "…"                # what the concept is, in detail; given to the LLM
    attributes: [description, status, resolution]
    events: [occurrence, cause, attempt]
    count_events: [occurrence]      # counted by code (frontmatter `occurrences`, indexes)
    description_from: [description]
    identity_rule: "…"              # what "the same entity" means (entity proposals)
    entity_proposals: true          # free names: status suggests `entities --propose`
    known_keys: ["acme/nightly-import-fails"]   # canonical keys the LLM must reuse (prompts with {known_keys})
    review_priority: normal         # high | normal | low
aliases: {"REF 5001": "tickets/7001"}
policy: {stale_after_days: 365, minimize_personal_data: true, quarantine_rules: default}   # minimize_personal_data: accepted, not implemented yet
grounding: {llm_check_sample: 0.0, review_reject_share: 0.3, review_min_rejected: 5}
images: {reader: off, ocr: {command: tesseract, languages: eng, timeout_seconds: 120}}   # off | ocr | vision
schedule: {mode: manual, every: 1d}
backup: {mode: manual, every: 7d, destination: ../backups, include_cache: true, keep: 5, max_age_days: 7}
prompts: {interpret: interpret-v3}  # default: the latest of the package
eval: {tolerances…}                 # see evaluate.py
```

The curator refuses to start with an invalid configuration. Extractors: `note`, `text`, `pdf`, `docx`,
`table`, `image`, `email`, `whatsapp_export`. The fallback by extension maps txt/md → text, pdf, docx,
xlsx/csv → table, png/jpg → image, eml/mbox/msg → email.

## 5. The LLM

The LLM is a **function**: system prompt + content + JSON schema in, a valid object out (one retry, then
the chunk is an error in the report).

**`claude-code` backend** (subscription), `llm/claude_code.py`:

```
claude -p --output-format json --json-schema <schema> --permission-mode dontAsk --tools "" \
       --safe-mode --strict-mcp-config --disable-slash-commands --no-session-persistence --system-prompt <prompt>
```

- content on stdin;
- run from an empty temporary folder, created for each call and deleted after;
- `ANTHROPIC_API_KEY` removed from the environment, so the subscription is used;
- `--safe-mode` excludes the user's CLAUDE.md, hooks, MCP servers and plugins;
- `doctor` checks that the flags exist in the installed version;
- images are never sent: `images.reader: ocr` gives the LLM text only, and `vision` is refused until a
  backend can see images in isolation.

**`anthropic-api` backend** (`llm/anthropic_api.py`, extra `api`): the official SDK; the JSON schema in
`output_config.format` (structured outputs); the system prompt marked for prompt caching; streaming with
`get_final_message()`; `llm.model` (default `claude-opus-5-5`), `llm.effort`, `llm.max_output_tokens`;
server-side fallbacks on refusals (`llm.fallbacks`, beta `server-side-fallback-2026-07-01`); `refusal` and
`max_tokens` stop reasons are errors for the chunk. Usage (input, output, cache write and read) and cost
from the response, with the price list of the current models or `llm.prices`. Credentials from the
environment (resolved by the SDK), never from files.

**`openai-compatible` backend** (`llm/openai_compat.py`, standard library HTTP): `POST {base_url}/chat/completions`
with `response_format: json_schema` (strict) and `temperature: 0`; a server that answers 400 to it gets
`json_object` with the schema in the system prompt (remembered for the rest of the run). Usage from
`usage.prompt_tokens/completion_tokens`; cost from `llm.prices` (0 by default: local models). Optional
key from the environment variable named in `llm.api_key_env`. `doctor` lists `GET {base_url}/models`.

**Estimates:** the dry run counts tokens from the text (about 3.5 characters per token plus the fixed part of
each call); with a known price it adds dollars, assuming three quarters input and one quarter output.

**`fake` backend:** rules `{match, response}` from JSON files; the only backend allowed in tests.

**Cache:** `state/llm_cache/<key[:2]>/<key>.json`. The key is a hash of the content of the request,
the prompt version, the model and the schema. Regeneration with the cache is free and deterministic.
`--no-cache` ignores it; `forget` removes the entries of what it deletes.

**Calls:**
1. **Interpretation**, one chunk at a time. The answer has:
   - `facts`: `concept_type` (enum of the types allowed for the source), `concept_key`, `field`
     (`{attribute: …}` or `{event: …}`, among those of the type), `text`, `kind` (fact or inferred),
     `quote`, `quote_lang`, `location` (an anchor of the chunk), `date`, `time_expression_raw`, `related`;
   - `unclassified`;
   - `suspicious_instructions`.

   The system prompt describes the types, their fields, identity rules and known keys. A header gives
   the title, type and date of the source.
2. **Reconciliation**, one concept at a time, several concepts per call. For each new fact: `new`,
   `duplicate_of`, `supersedes` (only with an explicit replacement in the source) or `conflicts_with`.
   The code checks ids and coherence: `supersedes` and `conflicts_with` only between facts of the same
   attribute, never between events. Identical facts are found by code without the LLM.
3. **Entities** (on request): for one type, the active concepts with a few facts each, and the identity
   rule. The answer is proposals `same` or `similar` with a reason. Decided pairs are not proposed again.
4. **Quote check** (optional, sampled: `grounding.llm_check_sample`): "does the quote support the fact?"
   An unsupported fact is rejected.

Prompts are versioned files; the version is written in every fact and report, and selectable per
instance (`prompts.interpret`). A new version is accepted only after `curator eval` on the whole set.

## 6. Quote verification (`grounding.py`)

A fact enters the store only if all of these hold:
1. the `location` exists in the chunk and contains the `quote` (normalized: spaces, case, accents,
   typographic quotes, ligatures, end-of-line hyphenation);
2. every number, code, date and proper name in `text` is in the quote, or in the rest of the **same
   sentence or table row** of that location, or in the title or date of the source. Comparison is done
   after normalization: dates in any written form, decimal separators unified;
3. `quote_lang` matches the language detected in the quote.

Dates:
- a date given by the LLM must be written in the block, else it becomes uncertain;
- relative expressions are computed by `temporal.py` from the date written in the block, else from the
  source date.

Rejected facts go to the report with the reason. Per chunk, the store counts proposed and rejected
facts; a source over the thresholds of `grounding` is listed for a check by hand.

## 7. Extraction and chunks

- **Anchors:** `p<N>` pages, `s-<slug>` sections, `r-<sheet>-<row>` table rows, `msg-<hash>` messages.
  `curated/<folder>/<doc id>.md` has the metadata (raw paths, hash, date and its origin, confidence,
  extractor version) and one `<a id="…"></a>` per location.
- **PDF:** pypdf. Pages without text go through the image reader. A scanned PDF whose images are nested
  in form objects is not read by pypdf: a known limit.
- **Word:** paragraphs and tables in reading order, sections from headings.
- **Tables:** one location per row, the header row repeated in every chunk, chunks never mix sheets.
- **Images:** OCR text as one location `p1`, confidence low. Images under 1500 px on the long side are
  enlarged 2× in grayscale first.
- **Mail:**
  - `.eml` and `.mbox` with the standard library; `.msg` with olefile (MS-OXMSG properties, date from the
    headers or from `PR_CLIENT_SUBMIT_TIME`);
  - one document per message, `m-<hash of the Message-ID>`, or a hash of sender, date, subject and body
    when the Message-ID is missing;
  - date from the `Date` header; thread from `In-Reply-To` and `References`;
  - quoted replies, signatures and disclaimers are removed;
  - readable attachments become documents (`d-<hash of the content>`), with a copy in
    `curated/attachments/`;
  - a changed mailbox is not a revision of its old messages.
- **WhatsApp export:**
  - Android and iPhone formats; day-first dates unless the file is clearly month-first;
  - multi-line messages joined, system messages dropped, omitted media noted;
  - one location `msg-<hash>` per message, with date, time and author;
  - the date of the source is the last message.
- **Chunks:** sections are kept whole when possible, up to `chunking.max_tokens` with a small overlap;
  table rows in groups of `table_rows_per_chunk`; manuals with `summarize: false` give only the allowed
  types.

## 8. Quarantine

The fixed rules (`quarantine.py`) look for:
- phrases addressed to an assistant ("ignore the previous instructions", "you are an assistant",
  requests to send, delete or execute);
- invisible characters;
- anomalous metadata.

The LLM's `suspicious_instructions` field is the second level. A flagged document is removed from staging
before anything is published. A false positive costs a `release`.

## 9. Safe writing

- All the work of a run happens in `state/staging/<run id>/`, on copies of the store and of `curated/`.
- Only a valid result is published: `curated/`, store and manifest, then `ssot/`, the search index, a
  commit and a report.
- A single-writer lock (`state/curator.lock`) prevents two writers.
- A run with no changes writes nothing.
- The curator writes only `curated/`, `state/` and `ssot/`; in `raw/` it only creates the missing empty
  folders of the type.

## 10. Derived states (`derive.py`)

At every run, from scratch, in this order:
1. concepts: merges, aliases, renames, links;
2. refiled facts;
3. code duplicates;
4. reconciliation judgments;
5. revisions of documents (same path, or `supersede-source`);
6. the owner's decisions, in order, by id or by anchor;
7. sources excluded with `ignore`;
8. deletion rules;
9. quarantine.

The result is deterministic: a regeneration equals the sequence of runs, given the same answers.

## 11. Claude Code in the instance

`init` (and `migrate` for older instances) writes:
- **`.claude/settings.json`:**
  - `allow`: the read-only commands with any argument (`Bash(curator status *)`, …), and `entities` and
    `doctor` only in their exact forms, because with arguments they could use quota;
  - `ask`: every command that changes something;
  - `deny`: `Edit(/raw/**)`, `Edit(/curated/**)`, `Edit(/state/**)`, `Edit(/ssot/**)`,
    `Edit(/curator.yaml)`.

  Rules are checked deny → ask → allow; `/` is relative to the settings file. On native Windows `Edit`
  rules stop the agent's file tools and the shell writes Claude Code recognizes, not a script that writes
  by itself; the sandbox of Claude Code exists on macOS, Linux and WSL2.
- **`.claude/skills/ssot/SKILL.md`:** first contact, normal session, commands by intention, rules. Keep it
  in step with the commands.

## 12. Backup, schedule, deletion

- **Schedule:** `schedule.mode` and `every`. `curator schedule` prints commands, never installs.
  "Overdue" compares the last finished `run` or `rebuild` with `every`.
- **Backup:**
  - a zip of the whole instance except staging, index and lock (cache optional), with a manifest of
    hashes and file dates;
  - written as `.part`, then renamed; rotation by `keep`;
  - periodic backup at the end of a run when due;
  - `--verify` extracts in a temporary folder and compares the hashes;
  - `restore` only into an empty folder; it restores file dates, creates the git repository if missing
    and rebuilds the search index.
- **Forget:**
  - rules in `state/forget.jsonl`: `{kind: source, source, file_sha256}` or `{kind: term, hash, words,
    mode}`;
  - a term matches a window of as many normalized words with the same hash;
  - `apply_rules` runs in every run on the staging copy;
  - `apply_to_document` runs before the LLM, also in `eval`;
  - `scrub_state` runs once, when the command is given: cache, reports, quarantine list, evaluation
    results, `decisions.jsonl`.

## 13. Evaluation (`evaluate.py`)

- A case is a real chunk of a file of `raw/`: `source` plus a `chunk` index or `anchors`. It uses the same
  request and cache key as `run`, so the evaluation right after a run is free.
- A case has:
  - `expect`: type (or list), `key` or `path` (globs allowed), `field`, `contains`/`any`, `date`;
  - `absent`: text, or a type and path for a concept that must not exist;
  - `complete: true` to count precision.
- Reconciliation cases are groups of hand-written facts with the expected relation.
- The metrics, tolerances and verdict are compared with `eval/baseline.json` (or `--baseline NAME`).
- Format and examples: `eval/README.md`.

## 14. Tests

They use only synthetic data (`curator/demo_files.py`, `curator/data/demo/`, `examples/`, `tests/`) and the `fake` backend, with time pinned by
`CURATOR_NOW`. Among them:
- a valid SSOT from the examples;
- identical copies as one document;
- page filters;
- quarantine never reaching the outputs;
- idempotence and byte-identical regeneration;
- missing and excluded sources;
- token limit and resume;
- the single-writer lock;
- invalid LLM output;
- quote checks: invented quote, number, code, date or name not in the quote, sentence-level values,
  language;
- relative dates;
- decisions surviving a regeneration with different LLM output;
- mail in three formats with threads and attachments;
- WhatsApp formats;
- OCR, real and fake;
- redaction with black boxes;
- schedule, backup with a byte-identical restore, forget;
- `init` writing valid Claude Code files;
- every type creating a valid instance;
- the extended examples giving exactly `examples/expected/ssot/`.

## 15. Adding things

- **A source:**
  1. a module in `extractors/` that returns sections with anchors;
  2. an entry in the extractor list and, if useful, in the fallback by extension;
  3. a rule in the types;
  4. a synthetic example with its fake LLM answer;
  5. a test.
- **A type:** a YAML file in `curator/data/types/` with `status: draft`. Mark it `tested` after use on
  real data with an evaluation set.
- **A prompt version:** a new file `prompts/<name>-vN.md`, selected per instance, kept only if
  `curator eval` on the whole set is not worse.
- **A command:** document it in this recipe, in the guide and in the `ssot` skill, and in the permissions
  of `.claude/settings.json`.

## 16. Versions and installation

- Semantic versions, git tags `vX.Y.Z`, `CHANGELOG.md`.
- `curator_version` in each instance; `curator migrate` for format changes (`CONFIG_STEPS`, schema of the
  store).
- Install with `uv tool install git+https://…[@vX.Y.Z]`, update with `uv tool upgrade ssot-curator`.
- The data of the package (`types/`, `prompts/`, `templates/`) are installed with it.
