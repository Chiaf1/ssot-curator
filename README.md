# ssot-curator

**Turn a folder of messy files into a single source of truth you can trust.**

You drop notes, documents, manuals, mail and chat exports into a folder. An LLM reads them and proposes
*facts*. The curator checks that every fact is backed by a quote that really exists in the source, sorts
the facts into concepts (machines, problems, procedures, requirements, tickets… whatever your *type* of
SSOT defines) and writes them as plain Markdown pages in [Open Knowledge Format (OKF)](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md),
with the history of what changed, what conflicts and where each fact comes from. Every change is a git
commit. You decide what is verified, what is the same thing, what to keep when sources disagree.

It helps whenever answers have to come from a pile of heterogeneous sources — *"what was done so far?"*,
*"what is the most frequent problem and how is it solved?"*, *"how do I configure X?"*, *"what is the
current state, and since when?"* — and you need to know which source each answer comes from.

## The easiest start: open it with Claude Code

You do not need to read further to begin. **Clone this repository and open the folder with
[Claude Code](https://code.claude.com)**:

```bash
git clone https://github.com/Chiaf1/ssot-curator.git
cd ssot-curator
claude
```

and ask, for example, *"what is this and how do I start?"*. The repository contains a skill
(`ssot-curator`) that makes the agent your guide. It explains what the tool does and where to be careful,
checks the requirements, installs the command (only with your consent), helps you choose the type of SSOT
and the LLM backend, and creates your first instance. Inside the instance, another skill (`ssot`) takes
over: it runs the curator with your confirmation and walks you through what needs a decision. The rest of
this README is the same information, written down.

> **Status: 0.4.4.** Three SSOT types, all **drafts**: `plant-support` (technical support of an industrial
> plant: axes, network devices, alarms, procedures, problems, service tickets) is the generic form of a type
> used for months on real data; `client-project` (study a client company and follow an engagement) and
> `software-support` have not been exercised on real data yet. Check the first results of any type and write
> a few evaluation cases (`curator eval`).

## Performance and known limits

**Slow on large volumes, for now.** Every block of every document is one LLM call, made one after the other:
about 30-45 seconds and 13,000-16,500 tokens per call with the `claude-code` backend. 1,000 small documents
take 11-17 hours and about 13 million tokens on the first load; after that, only new documents cost.
The measured numbers, how to estimate a load, what you can do today and the improvements we are
evaluating (parallel calls, larger blocks, compact answers, incremental work) are in
**[docs/performance.md](docs/performance.md)**.

## How it works

```
raw/ (your files, read-only for the curator)
  → extraction (text, PDF, Word, Excel/CSV, mail, WhatsApp export, images with OCR)
  → quarantine of suspicious content (e.g. "ignore the previous instructions")
  → the LLM proposes facts, each with a quote and its location
  → the code verifies every quote (numbers, codes, names and dates must be in the source)
  → reconciliation per concept: duplicate, superseded, conflict
  → your decisions (approve, reject, resolve, merge, …), kept across regenerations
  → ssot/ (Markdown, OKF), validated and committed in the git repository of the instance
```

- **Traceable:** every fact cites its source and the place in it (page, row, message).
- **No silent invention:** a fact whose numbers, codes, names or dates are not in its quote is rejected.
- **History, not overwrite:** what is superseded stays, with the date and the newer fact.
- **One writer:** only the curator writes the SSOT; people and agents read it and give decisions.
- **Rebuildable:** from `raw/`, the decisions and the LLM cache, a regeneration gives the same SSOT at no cost.

## Requirements

- Python 3.12 or newer, and git.
- [uv](https://docs.astral.sh/uv/) (or pipx) to install the command.
- An LLM backend (`llm.backend` in `curator.yaml`), chosen per instance. The features are the same with
  all of them:

  | Backend | Status | What you need | Cost | Where your text goes |
  |---|---|---|---|---|
  | `claude-code` | **tested on real data** | [Claude Code](https://code.claude.com) installed and logged in with a Claude subscription (Pro or Max) | the quota of the subscription (estimates in tokens) | Anthropic, under the terms of your plan |
  | `anthropic-api` | ready, **not yet tested on real data** | an Anthropic API key in `ANTHROPIC_API_KEY`; install with the `api` extra | per token (estimates and costs in $) | Anthropic, under the API terms |
  | `openai-compatible` | ready, **not yet tested on real data** | an OpenAI-compatible endpoint: an **open-weight model on your own computer or server** ([Ollama](https://ollama.com), vLLM, LM Studio, llama.cpp) or a hosted service | free locally (or `llm.prices`) | **nowhere else** with a local server |

  > **Only `claude-code` has been used on real data so far.** `anthropic-api` and `openai-compatible` are
  > implemented and covered by automated tests that simulate the service, but they have never run against
  > a real API key or model server: expect to need small adjustments (a parameter the service rejects,
  > output limits, a model that follows the JSON schema poorly). Start with `curator doctor --llm-check`,
  > then a small `curator run --only "<one file>"` and `curator eval`, and please report what you had to change.

  For real or confidential data, an API contract or a local open-weight model is usually the right
  choice; the subscription suits trials and personal use (check its terms for automated processing).
  Quality depends on the model: measure it with `curator eval` before relying on a new one.
- Optional: [Tesseract](https://github.com/tesseract-ocr/tesseract) to read scans and screenshots
  (`curator doctor` tells you how to install it and its languages).

## Install

```bash
uv tool install git+https://github.com/Chiaf1/ssot-curator.git            # latest
uv tool install git+https://github.com/Chiaf1/ssot-curator.git@v0.4.1     # a chosen release (tags in CHANGELOG.md)
curator --version
```

Update with `uv tool upgrade ssot-curator`, then run `curator migrate` in each instance (see
[Updating](#updating)). With pipx: `pipx install git+https://github.com/Chiaf1/ssot-curator.git`.
For the `anthropic-api` backend install the extra:
`uv tool install "ssot-curator[api] @ git+https://github.com/Chiaf1/ssot-curator.git"`.

### Choosing the backend

```yaml
# curator.yaml — one of:
llm: {backend: claude-code}                                   # subscription; model: "" = default of Claude Code
llm: {backend: anthropic-api, model: claude-opus-5-5}         # also claude-sonnet-5-5, claude-haiku-5-5; effort: low…max
llm: {backend: openai-compatible, model: qwen3:32b, base_url: "http://localhost:11434/v1"}   # Ollama on this computer
llm: {backend: openai-compatible, model: my-model, base_url: "https://…/v1", api_key_env: MY_KEY,
      prices: {input_per_mtok: 0.5, output_per_mtok: 1.5}}                                    # a hosted service
```

`curator init --llm <backend>` writes it for you; `curator doctor` checks it without spending anything
(`--llm-check` makes one tiny real call). Answers are always validated against a JSON schema: the
Claude API and most servers enforce it (structured outputs); with a server that does not, the schema goes
into the prompt and invalid answers are retried and then reported.

## Try it without using quota

```bash
curator demo my-demo
```

creates an instance of the `plant-support` type filled with **synthetic files** (an invented bottling line:
a service ticket and a report, a manual, a note, a table, a mail thread with an attachment, a WhatsApp
chat, a screenshot, and two files with instructions aimed at an LLM), runs the whole cycle with a **fake
LLM** that replays stored answers, and prints the run report, the status and the pages worth opening in
`my-demo/ssot/`. Nothing is sent anywhere and no quota is used. The stored answers contain mistakes on
purpose: the run shows the facts the verification rejected and the files held in quarantine. Then try
`curator review`, `curator search "termination resistor"` or `curator approve <fact id>` in that folder,
or open it with Claude Code.

## Quick start

```bash
curator types                                                               # the available types
curator init --type client-project --language en --llm claude-code my-ssot   # choose a type and a backend
cd my-ssot
# put your files in the folders of raw/ (see below)
curator doctor                          # checks configuration, git, Claude Code, OCR, backup
curator run --dry-run                   # estimates the LLM use, calls nothing
curator run --max-tokens 200000         # processes the files, with a spending limit
curator status                          # what happened, what waits for you
```

Then **open the folder with Claude Code**: the instance contains a skill (`ssot`) and permissions that
make the agent your guide. It explains the tool, runs it with your confirmation, summarizes the results
and walks you through what needs a decision, one item at a time. It cannot write the SSOT itself.

## Where to put the files

`curator init` creates the folders of the type in `raw/`, and every run recreates them if they are
missing. **A folder says how its files must be read, not what format they are**: inside a folder, the
format is recognized by the extension (PDF, Word, Excel/CSV, mail, WhatsApp export, text, images). For
`client-project`, for example: `notes/` (your own notes), `interviews/` (each statement attributed to who
said it), `conversations/` (mail and chat with the client), `company-docs/` (official documents),
`finance/` (P&L, budgets: one fact per line item), `project/` (proposals, plans, minutes). Each folder has a
**reading hint** that is given to the LLM with every block of its files; the rules in `curator.yaml`
(`sources:`) can also limit the kinds of concept a folder may produce (e.g. only procedures from `manuals/`)
or the pages of a PDF. **A file outside every folder is read by its extension**, with every kind of concept
allowed and no hint, and the report says so. Files with unknown extensions are listed as unsupported, never
read.

The curator **never changes `raw/`**: to stop using a file, add an `ignore` rule to `curator.yaml` (its
facts are withdrawn, not deleted, and come back if you remove the rule); to delete it for privacy or legal
reasons, use `curator forget`.

## Everyday use

| You want to… | Command |
|---|---|
| see the state | `curator status` · `curator doctor` |
| process new files | `curator run --dry-run`, then `curator run --max-tokens N` |
| look something up | `curator search "text" [--type T]` (or read `ssot/` in any Markdown viewer, read-only) |
| verify facts | `curator review` → `curator approve <fact or concept>` / `curator reject <fact>` |
| settle a conflict | `curator conflicts` → `curator resolve <id> --keep A\|B\|both --note "…"` |
| join near duplicates | `curator entities --propose --type T` → `merge` / `link` / `separate` |
| move a misfiled fact | `curator refile <fact>… --to <concept>` |
| suspicious source | `curator quarantine` → `release <id> --as-data --note "…"` / `discard <id>` |
| run it regularly | set `schedule:` in `curator.yaml`; `curator schedule` prints the cron / Task Scheduler line |
| back up | `curator backup`, `curator backup --verify`, `curator restore <zip> <new folder>` |
| delete data (privacy, legal) | `curator forget --source <id> \| --term "text"` |
| check quality after a change | `curator eval` |

The full guide is in [docs/guide.md](docs/guide.md).

## Costs and limits (read this)

- **LLM use.** Every new document is read by the LLM; the cache makes repeated work free. Always run
  `--dry-run` first: it estimates tokens and, when the price is known (Claude API, `llm.prices`), dollars.
  **The estimate is usually 20-25% below the real use.** `--max-tokens` stops a run cleanly; the next run
  resumes. Real runs report tokens and, with the API backends, the cost. With `claude-code` the quota is
  shared with your normal use of Claude, and you must check the terms of your plan for automated processing.
- **Large tables and long manuals** are expensive and give many repetitive facts. If a source gives many
  rejected facts, `status` lists it under *sources to check by hand*: keep it, fix it or exclude it.
  Do not tune the whole instance to one difficult file.
- **Images and scans** are read only if you choose so (`images.reader`: `off`, `ocr` with local
  Tesseract, `vision` not available yet). OCR misreads small text and complex layouts; its facts have low
  confidence and come first in the review.
- **Names chosen by the LLM** (a problem named by its symptom, a requirement by its need) can produce
  near duplicates: `status` tells you when new ones appeared since the last merge proposals.
- **Types are drafts**: check the first results and write a few evaluation cases. A type is plain YAML
  (`curator.yaml` of the instance): adapt its concepts, folders and hints to your subject, or write your
  own type from the closest one (`curator types --show`, `curator types --check my-type.yaml`, `curator init
  --type my-type.yaml`; see the guide, "Your own type"). Opened with Claude Code, this repository helps you
  design it from the questions your SSOT must answer.
- **Personal data:** `client-project` identifies people by name, to answer "who said what and when". If
  you may not keep names, switch the key of `Person` to a role (see the comment at the top of the type).

## Updating

`uv tool upgrade ssot-curator`, then in each instance `curator doctor` (it warns when the instance was made by
an older curator) and `curator migrate` (configuration, store of facts, Claude Code files; your comments
in `curator.yaml` are kept). An instance made by a newer curator is refused.

## Trying it on real data safely

The redaction tool has moved to its own repository, **[ssot-redact](https://github.com/Chiaf1/ssot-redact)** (until 0.4.1 it was
`tools/prepare_raw.py` here). It is installed separately and depends on this one; the curator works without
it. It makes a redacted copy of a folder of real files — names, customers, emails, phone numbers, in the
original formats, black boxes on images — straight into the `raw/` of an instance, and verifies it:

```bash
uv tool install git+https://github.com/Chiaf1/ssot-redact.git
ssot-redact init redact.yaml --sample ../originals --destination my-instance/raw
```

Its README and guide explain the workflow; with Claude Code, its `ssot-redact` skill guides you.

## Documentation

- [docs/guide.md](docs/guide.md) — user guide: workflow, decisions, costs, images, backup, deletion, troubleshooting.
- [docs/performance.md](docs/performance.md) — performance on large volumes: known limits, estimates, plans.
- [docs/architecture.md](docs/architecture.md) — the rules of the SSOT and how the curator keeps them.
- [docs/recipe.md](docs/recipe.md) — implementation: modules, configuration, commands, tests.
- [CHANGELOG.md](CHANGELOG.md).

## Warnings

- **The SSOT does not replace the original documents**, above all for safety procedures, parameters and
  anything that can hurt people or equipment: always check the original.
- You are responsible for the data you put in it and for the tool you process them with (an LLM service
  receives the text of your files). Personal data of other people need a legal basis.
- Choose the LLM backend **before** putting real data in: with `claude-code` or `anthropic-api` the text of
  your files goes to Anthropic; with a local open-weight model it stays on your machines.
- With a subscription instead of the API, check the terms of use of your plan for automated processing.

## License

Code: [MIT](LICENSE). Documentation: [CC BY 4.0](LICENSE-docs).
Main dependencies: pypdf (BSD), python-docx (MIT), openpyxl (MIT), PyYAML (MIT), jsonschema (MIT),
click (BSD), Pillow (MIT-CMU), olefile (BSD); optional: anthropic (MIT) for the `anthropic-api` backend,
Tesseract (Apache-2.0) as a system program.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest -q
```

Install the secrets check as a pre-commit hook once: `python tools/check_secrets.py --install-hook`
(it blocks API keys, tokens, private keys and passwords in staged files).

Tests use only synthetic data (`curator/demo_files.py`, `examples/`) and the `fake` LLM backend. See [CLAUDE.md](CLAUDE.md) for the rules
of the code.
