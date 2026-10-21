# User guide

This guide is for the **owner of an instance**: the person who puts the files in, runs the curator and
takes the decisions. The rules behind the SSOT are in [architecture.md](architecture.md); the
implementation in [recipe.md](recipe.md).

## 1. Concepts in two minutes

- **Instance:** a folder with your SSOT. It has its own git repository and these folders:
  - `raw/` — your files, untouched;
  - `curated/` — the text extracted from them, with one anchor per citable location;
  - `state/` — the store of facts, the LLM cache, the decisions and the run reports;
  - `ssot/` — the SSOT itself, Markdown pages in OKF format.
- **Type:** the kind of SSOT. It sets which concepts exist (e.g. *Problem*, *Procedure*, *Requirement*),
  their fields, the folders of `raw/` and the domain rules. Types (all drafts): `plant-support`
  (technical support of an industrial plant, the generic form of a type used on real data), `client-project`
  (study a client company and follow an engagement) and `software-support`. Each instance copies its type into `curator.yaml`,
  which you can then adapt.
- **Fact:** one statement with a **quote** and a **location** in a source. Its status is `pending`
  (proposed by the LLM), `verified` (by you), `superseded` (a newer fact replaced it), `conflicting`
  (two sources disagree) or `rejected`.
- **Attributes and events.** An attribute has one current value (e.g. the firmware of a motor); a newer
  value supersedes the older one. Events accumulate (an occurrence of a problem, an action, a meeting).
- **Concept:** a page in `ssot/` (e.g. `problems/line-1/motor-loses-communication.md`), with its facts,
  its history and the list of its sources.
- **Decision:** something you tell the curator with a command. It is recorded in
  `state/decisions.jsonl` with an *anchor*, so it is applied again after any regeneration.

## 2. First instance

To see the whole cycle first, without an LLM and without spending anything: `curator demo my-demo`
creates a `plant-support` instance from synthetic files, runs it with stored answers (backend `fake`) and
tells you which pages of `my-demo/ssot/` to open. The stored answers contain mistakes on purpose (a fact
with an invented quote, a wrong date, a file with instructions for the LLM): the report shows how the
checks stop them. The demo instance is an ordinary instance: every command works on it.

For your own data:

```bash
curator init --type client-project --language en --llm claude-code my-ssot
cd my-ssot
curator doctor
```

`init` does the following:
- copies the type into `curator.yaml`;
- creates the folders;
- writes the Claude Code files (`.claude/settings.json` and the `ssot` skill);
- writes an `ssot/` with its `CLAUDE.md`, the instructions for agents that read it;
- initializes git.

`doctor` checks:
- the configuration;
- the git repository;
- the backend:
  - `claude-code`: Claude Code installed, its flags, `ANTHROPIC_API_KEY` removed from its environment so
    that the subscription is used;
  - `anthropic-api`: the SDK installed, credentials present (never printed), the price of the model known;
  - `openai-compatible`: the server answers and offers the model (no tokens used);
- the image reader and Tesseract;
- the schedule;
- the age of the backup;
- whether the instance needs `curator migrate`.

`curator doctor --llm-check` also makes a tiny real call, which uses a little quota or credit.

### Choosing the LLM backend

Set `llm.backend` in `curator.yaml` (or `curator init --llm`). All the features are the same; what changes
is who runs the model, what it costs and where your text goes.

| Backend | Setup | Cost and estimates | Your text goes to |
|---|---|---|---|
| `claude-code` | Claude Code installed and logged in with a subscription | subscription quota; estimates in tokens | Anthropic (terms of your plan) |
| `anthropic-api` | `ANTHROPIC_API_KEY` in the environment; install with the `api` extra; `llm.model` (default `claude-opus-5-5`; also `claude-sonnet-5-5`, `claude-haiku-5-5`), optional `llm.effort` | per token; dry runs estimate dollars, runs report the real cost | Anthropic (API terms) |
| `openai-compatible` | `llm.model` and `llm.base_url` of an OpenAI-compatible server: an open-weight model on your computer or network (Ollama `http://localhost:11434/v1`, vLLM, LM Studio, llama.cpp), or a hosted service with `llm.api_key_env` | free locally; `llm.prices` for a paid service | **your own machines** with a local server |

Things to know:
- **Tested so far: only `claude-code`.** The other two backends are covered by tests that simulate the
  service, never by a real API key or model server: the first runs may need small adjustments. Begin with
  `curator doctor --llm-check`, one file (`curator run --only "<path>"`) and `curator eval`.
- **Structured answers.** The curator always asks for one JSON object that matches a schema, and checks it.
  The Claude API and most servers enforce the schema; with a server that does not, the schema is written
  into the prompt, and an invalid answer is retried once and then reported for that chunk.
- **Output length.** `llm.max_output_tokens` (default 32000) bounds one answer of the API backends; long
  tables can need more, or smaller chunks (`chunking.max_tokens`).
- **Refusals.** With `anthropic-api`, a request the model declines is re-run on another model on the
  server side (`llm.fallbacks: true`, the default); a final refusal is reported for that chunk.
- **Quality depends on the model.** Smaller open-weight models follow long instructions less well: more
  facts rejected by the quote check, more near duplicates. Write a few evaluation cases and run
  `curator eval` before relying on a new model. The cache is per model: changing model re-reads documents.

Things you may want to change in `curator.yaml` before the first run:
- `instance.language` — the language of the facts. Quotes stay in their original language.
- `llm.max_tokens_per_run` — the default spending limit of a run.
- `images.reader` — `off` by default (see §7).
- `ignore` — files never to read.
- `backup.destination` — where backups go (see §9).
- `known_keys` of a concept type — canonical names the LLM must reuse (e.g. the names of problems you
  already know).

### Your own type

When no installed type fits, write your own: one YAML file, with the same rules as the installed ones.

```bash
curator types --show plant-support > my-type.yaml   # start from the closest type
# edit: first comment line (what it is for), status: draft, instance.type, concepts, sources
curator types --check my-type.yaml                  # every error explained, plus warnings
curator init --type my-type.yaml --llm claude-code my-ssot
```

Design it from the questions the SSOT must answer: write 8-10 of them, then give each kind of concept a
reason to exist (the questions it serves). For each concept type: a CamelCase name, a `folder`, a `key` with
`{variables}` (how one concept is told from another), `attributes` (one current value) and `events`
(dated, they add up), a one-line `summary` for the readers of the index pages, a `description` for the
LLM (as detailed as needed) and an `identity_rule`; `entity_proposals: true` when the
LLM names concepts freely (a symptom, a need). For each folder of `raw/`: how its files must be read
(`hint`), and `extract:` if it may give only some concept types. `curator types --check` refuses what
would break (unknown keys, invalid names, a field both attribute and event, `count_events` that are not
events…) and warns about what the LLM would miss (no description, no hint). `curator init` refuses a type
with errors. The `ssot-curator` skill of the repository walks you through the whole design.

## 3. Putting files in `raw/`

Put each file in the folder of `raw/` that says **how it must be read** (an interview, a manual, a
financial report…), whatever its format. The `sources:` rules in `curator.yaml` decide, for each folder:

- the **extractor**: `auto` (the usual choice: by the extension of each file, and a `.txt` that is a
  WhatsApp export is recognized as such) or a fixed one (`note`, `text`, `pdf`, `docx`, `table`, `image`,
  `email`, `whatsapp_export`); the first rule that matches a file wins, so a fixed rule for some
  extensions can come before an `auto` rule for the rest of the folder;
- the **reading hint** (`hint:`): one or two sentences given to the LLM with every block of these files
  (e.g. "attribute every statement to the person who said it"). A changed hint applies to the files
  read from then on; `curator rebuild` applies it to the files already read (new LLM calls for that
  folder, since the prompt changed);
- which **concept types** may be extracted (`extract:`; e.g. only procedures from manuals);
- for PDFs, which **pages** (`pages:`; useful for long manuals);
- whether the LLM also summarizes (`summarize:`).

A file outside every rule is read **by its extension**, with every concept type allowed, and the run
report says "read by extension". Copies of the same file in different folders are one document. A file
still being copied (modified less than a minute ago) is skipped and taken at the next run.

What each extractor gives:

| Source | Citable location | Notes |
|---|---|---|
| text, notes (`.txt`, `.md`) | section `s-…` | notes are attributed to the owner |
| PDF | page `p<N>` | pages without text read with OCR if `images.reader: ocr` |
| Word | section `s-…` | tables kept as tables |
| Excel / CSV | row `r-<sheet>-<row>` | one location per row, header repeated |
| mail (`.eml`, `.mbox`, `.msg`) | message `msg-…` | one document per message; same message in two files = one document; quoted replies, signatures and disclaimers removed; attachments become documents |
| WhatsApp export (`.txt`) | message `msg-…` | Android and iPhone formats; date, time and author in each location |
| images (`.png`, `.jpg`) | `p1` | only with `images.reader: ocr`; low confidence |

The date of a source comes from its content, else from its file name, else from the date of the file
(`source_date:`). For mail, the `Date` header is used; for a chat, the last message.

## 4. Running

```bash
curator run --dry-run                  # what would be done, with a token estimate; nothing is written
curator run --max-tokens 150000        # the real run, with a limit
curator status
```

A run goes through these steps:
1. scans `raw/`;
2. extracts the new documents;
3. puts suspicious ones in quarantine;
4. asks the LLM for facts, block by block (cached);
5. verifies every quote;
6. reconciles each concept that received facts (cached);
7. applies your decisions;
8. writes `ssot/`;
9. validates and commits.

If the limit is reached the run stops cleanly and publishes what is done; the next run resumes.
`curator rebuild` reprocesses everything from `raw/`: with the cache it costs nothing, unless a prompt,
the model or a source changed.

**Estimates.** The dry-run estimate counts the input precisely but cannot know the length of the
answers: **real use is usually 20-25% higher**. Long tables are the most expensive sources, because one
row gives several facts.

## 5. Reviewing and deciding

`curator status` lists what waits for you:
- facts to verify;
- open conflicts;
- quarantined sources;
- sources to check by hand (many facts rejected by the quote check: often an unusual layout, an OCR
  problem or a reduced copy of a document);
- new concepts since the last entity proposals;
- missing sources;
- backup age;
- a run that is overdue.

| Situation | What to do |
|---|---|
| a fact is right | `curator approve <fact_id>` (or a whole concept: `approve <path or uid>`) |
| a fact is wrong | `curator reject <fact_id>` |
| two sources disagree | `curator conflicts`, then `curator resolve <id> --keep A\|B\|both --note "why"`. Often the newer source simply updates the older one: keep the newer. |
| two concepts are the same thing | `curator merge <source> <target>`: the source becomes a page that points to the target, and its name becomes an alias |
| two concepts are related but different | `curator link <a> <b>` (grouped in the index) |
| a merge proposal is wrong | `curator separate <a> <b>` (it is not proposed again) |
| a fact is under the wrong concept | `curator refile <fact_id>… --to <concept>` |
| a concept has a bad name | `curator rename <uid> <new path>`, `curator alias <uid> "other name"` |
| a document is a newer version of another | `curator supersede-source <old> <new>` (`curator revisions` lists likely pairs) |
| a source is in quarantine | read the reason with `curator quarantine`; `release <id> --as-data --note "…"` if it is legitimate, `discard <id>` otherwise |
| a file should not be read any more | add it to `ignore` in `curator.yaml`: its facts are withdrawn (not deleted) |

**Near duplicates.** Concepts whose names the LLM chooses freely (types with `entity_proposals: true`,
e.g. problems named by their symptom) can appear twice with slightly different names. After runs that
add such concepts, `status` says so: run `curator entities --propose --type <Type>` (one LLM call) and
decide each proposal with `merge`, `link` or `separate`.

**Do not tune everything to one file.** When one source gives many wrong or rejected facts, the remedy is
on that source (check it, fix it, exclude it), not a change of prompts or rules for the whole instance:
such changes are measured on the whole evaluation set (§10) and usually make other sources worse.

## 6. Working with Claude Code

Open the instance folder with Claude Code. The `ssot` skill tells the agent:
- what the tool does and how to explain it;
- the normal session: status → dry run → run with your confirmation → summary → review;
- the commands for each intention of yours;
- the rules: it never writes the SSOT or `raw/`, never follows instructions found in the data, shows only
  the reference of a quarantined source, and asks before every command that changes something or uses
  quota.

`.claude/settings.json` enforces part of this:
- read-only commands are allowed;
- the others ask;
- editing `raw/`, `curated/`, `state/`, `ssot/` and `curator.yaml` is denied.

On native Windows these rules stop the agent's file tools and the shell writes Claude Code recognizes
(redirections, `tee`, `sed`), but not a script that writes by itself. The operating-system sandbox of
Claude Code exists on macOS, Linux and WSL2. For stronger protection in production, make `raw/`
read-only for the user that runs the agent.

To ask questions about the content, any agent can read `ssot/` following `ssot/CLAUDE.md`:
- start from `ssot/index.md` or `curator search`;
- cite concepts and sources with their location;
- say what is verified, pending, inferred or in conflict;
- say clearly when something is not in the SSOT.

## 7. Images and scans

`images.reader` in `curator.yaml`:

| Value | What it does | Good | Watch out |
|---|---|---|---|
| `off` (default) | images and scanned pages are not read | no cost, no risk | they give no facts |
| `ocr` | local Tesseract; the text becomes a normal document for the LLM | no tokens to read, no image leaves the computer | small fonts, layouts, handwriting are read badly; facts have low confidence |
| `vision` | a model that sees the image | understands layouts | not available yet |

For `ocr`, install Tesseract and the languages of your documents. Then set them in `curator.yaml`:

```yaml
images:
  reader: ocr
  ocr:
    command: tesseract          # or the full path, e.g. C:/Program Files/Tesseract-OCR/tesseract.exe
    languages: eng              # e.g. ita+eng
```

`curator doctor` checks the program and the languages. Small images (screenshots) are enlarged before
OCR. Screenshots that only *show* something (a status window, an error dialog) usually give no facts.
That is correct, and they cost an LLM call each, so exclude the ones you do not need.

## 8. Running regularly

```yaml
schedule:
  mode: periodic     # or manual (default)
  every: 1d          # 30m, 12h, 1d, 2w
```

The curator installs nothing in your system. `curator schedule` prints the line to add to `crontab -e`,
the `schtasks` command for Windows, or what to set in a scheduled task of the Claude Code desktop app.
`doctor` and `status` warn when a periodic run is overdue. You can always run by hand.

## 9. Backup and restore

```yaml
backup:
  mode: periodic       # or manual (default): with periodic, a run makes the backup when it is due
  every: 7d
  destination: ../ssot-backups   # relative to the instance, or absolute; never inside raw/
  include_cache: true            # the LLM cache is costly to recreate
  keep: 5
  max_age_days: 7                # doctor warns after this
```

- `curator backup` writes `ssot-backup-<instance>-<date>.zip`. It holds the whole instance, except the
  staging folder, the search index and the lock: `raw/`, decisions, deletion rules, evaluation set,
  configuration, the store of facts (with the history), `curated/`, the git repository and the cache.
  A manifest inside records the hash and the date of each file.
- `curator backup --verify` is the **restore test**: it extracts the latest backup in a temporary folder
  and compares every hash. Do it regularly; `doctor` reminds you if it was never done.
- `curator restore <zip> <new folder>` gives back the same instance, with the same SSOT, file dates and
  git history, and no LLM call. It never writes over an existing folder.
- Follow the **3-2-1 rule**: three copies, on two different media, one off site. Point `destination` to
  another disk or a network share, and copy the archives off site with your usual tools.

## 10. Measuring quality

The `eval/` folder of an instance holds hand-written cases: real blocks of your sources with the facts
you expect (and the ones that must *not* appear), plus reconciliation cases. `curator eval` measures:
- recall;
- date accuracy;
- the share of facts rejected by the quote check;
- forbidden facts;
- reconciliation accuracy, false conflicts and missed duplicates;
- the number of distinct concepts against the expected one.

It compares the result with a saved baseline and gives a verdict. After a run the evaluation costs
almost nothing (same cache). Use it before keeping any change of prompt, model or configuration, and
always on the **whole** set, never only on the case that motivated the change. The format of the cases
is in `eval/README.md` of this repository.

## 11. Deleting data (privacy, legal obligations)

`curator forget` is the only command that deletes, and it asks for confirmation:
- `curator forget --source <doc id | raw/path>` deletes a source: its facts, its text in `curated/`, its
  LLM cache entries and the concepts left without facts;
- `curator forget --term "a name" [--mode redact|exclude]` removes a term everywhere: in facts, texts,
  cache, run reports and decisions. Before the LLM, new texts hide it (`redact`) or drop the passage that
  contains it (`exclude`).

Only **hashes** are recorded (`state/forget.jsonl`), and the rules apply at every run and rebuild, so the
data does not come back even if the file is still in `raw/`. The command prints the steps left to you:
- remove or redact the file in `raw/`;
- rewrite the history of the git repository of the instance (e.g. `git filter-repo`) and of its clones;
- replace the older backups.

After a deletion, the concepts that lost facts are compared again at the next run, which uses a little
quota.

## 12. Trying it on real data safely

Before sending real files to an LLM service, you can test the tool on a redacted copy:

```bash
python tools/prepare_raw.py redact.yaml          # first time: copy + redact + verify
python tools/prepare_raw.py redact.yaml --add    # later: only the listed files not yet copied
python tools/prepare_raw.py redact.yaml --verify # verification only
```

`redact.yaml` lists the files to copy (`files: [{from, to}]`), the substitutions (`substitutions:
[["Real Name", "Technician 1"], …]`, longest forms first), extra terms to look for (`forbidden_extra`),
capitalized words that are fine (`allowed`) and, for images and scans, `ocr: {command, languages}`.
**Keep it outside any repository and outside `raw/`**, because it contains the real names.

What the tool does:
- txt, Word and Excel files are redacted in their own format;
- PDFs with text are rebuilt as text only, one page per original page, so citations by page stay valid;
- images and scanned PDFs get black boxes on the words to hide (also names over several words, emails,
  phone numbers), and their metadata (EXIF, GPS) are dropped.

The report (`redact-report.md`) lists:
- replacements and black boxes;
- forbidden terms still present;
- emails, phone-like numbers and capitalized words to review;
- for images, the words read with low OCR confidence. OCR can misread a name, which then is not covered:
  look at the images yourself.

Run it, read the report, look at the files, and only then run the curator. The tool lives in a clone of
this repository: install it there with `pip install -e ".[prep]"` (the `prep` extra adds pypdfium2), plus
Tesseract for images.

## 13. Troubleshooting

| Symptom | Likely cause / what to do |
|---|---|
| `doctor` errors on the backend | Claude Code not installed or not logged in; a required flag missing in an old version: update Claude Code |
| a source is `missing` and `rebuild` refuses to start | a file disappeared from `raw/` without `forget`: restore it from the backup, or `forget` it |
| many facts rejected in one source | see "sources to check by hand"; check the source (layout, OCR, reduced copy) |
| facts with wrong dates | a relative date ("tomorrow") is computed by the code from the date written in the block or the date of the source: check `source_date:` and the dates in file names |
| a concept named oddly or twice | `rename`, `alias`, `merge`; add canonical names to `known_keys` |
| `search` says the index is missing | it is rebuilt by the next `run` or by `curator render` |
| the instance was made by an older curator | `curator migrate` |
| a run was interrupted (lock left) | if no curator is running, delete `state/curator.lock` |
