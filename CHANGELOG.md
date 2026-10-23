# Changelog

All notable changes to `ssot-curator`. Versions follow [semantic versioning](https://semver.org/):
a change of the instance formats comes with `curator migrate`.

## 0.4.2

- **The redaction tool has moved to its own repository, [ssot-redact](https://github.com/Chiaf1/ssot-redact)**,
  which depends on the curator; the curator works without it. `tools/prepare_raw.py`, `tools/pdfwrite.py`
  and the `prep` extra are removed. In its new home it works on whole folders, lists the names to redact
  (`discover`), replaces emails and phone numbers by itself, redacts mail and skips the formats it cannot
  redact instead of copying them. README, guide and the `ssot-curator` skill point to it.
- Examples: files with a fixed date, so the expected SSOT no longer changes with the day of the build.

## 0.4.1

- Examples: the same bytes on every system and at every build. The WhatsApp chat had CRLF line ends on
  Windows (another source id than on Linux and macOS, so the test of the examples failed there); the
  mailbox had the line ends of the system, the build time in its `From ` lines and a random MIME boundary.
  The expected SSOT now has the id the chat gets everywhere.
- `curator types --check` and `curator init` list every error at once: the rules between fields no longer
  stop at the first broken one.

## 0.4.0

- **`curator demo <folder>`:** a `plant-support` instance made of the synthetic examples, run with a fake
  LLM that replays stored answers: the whole cycle at no cost, with the facts the checks rejected, the
  files held in quarantine and the pages worth opening. The examples and the stored answers are now data
  of the package.
- **Your own types:** `curator init --type my-type.yaml` accepts a type file of your own, checked with the
  same rules as the installed ones; `curator types --check <type|file>` lists every error, explained, and
  the warnings; `curator types --show <type>` prints a type to start from. The welcome skill walks you
  through designing a type from the 8-10 questions your SSOT must answer.
- **`summary`** of a concept type: one line for the readers of the index pages; `description` stays the
  detailed text for the LLM. Instances without it keep showing the description.
- Installed types: every one passes `types --check` without warnings (identity rules added;
  `software-support` now has folders by reading mode with hints: `notes/`, `tickets/`, `conversations/`,
  `documents/`, `manuals/`).
- README: the install section is back to `latest` plus an example of a chosen release.

## 0.3.1

- Examples: the Word file is the same on every operating system (zipfile recorded the system that built
  it, so the source id changed on Linux and macOS and the test of the examples failed there).
- README: the install commands pick the newest release tag by themselves.

## 0.3.0

- **Folders say how to read, not what format:** `sources[].extractor: auto` picks the extractor by the
  extension of each file (a `.txt` that is a WhatsApp export is recognized as one), and `sources[].hint`
  is a reading instruction given to the LLM with every block of the folder's files (e.g. "attribute every
  statement to the person who said it").
- **Types:** `plant-support` (new, English, draft) is the generic form of the type used on real data for
  technical support of an industrial plant: plants, components, axes, network devices, data points,
  parameters, alarms, procedures, problems, service tickets. It replaces `assistenza-impianto`, which was
  tied to one installation: instances made with it keep working (an instance keeps its type in
  `curator.yaml`). `client-project` is rewritten to study a client company and follow an engagement:
  company, units, processes (with maturity), systems, P&L items, rules, people (who said what and when),
  project, requirements, decisions, issues, actions; folders by reading mode (notes, interviews,
  conversations, company documents, finance, project).
- **Examples:** a new neutral scenario (an invented bottling line BL2); the example PDFs, Word files and
  images are stored uncompressed, so they have the same bytes (and source ids) with every zlib version.
- README and guide: only `claude-code` is tested on real data; the other backends may need adjustments.

## 0.2.0

- **Two new LLM backends, same features:** `anthropic-api` (the Claude API through the official SDK, extra
  `api`: structured outputs, prompt caching of the system prompt, server-side fallbacks on refusals,
  usage and cost from each response) and `openai-compatible` (any OpenAI-compatible endpoint: an
  open-weight model on your own machines with Ollama, vLLM, LM Studio or llama.cpp, or a hosted service;
  JSON schema enforced when the server supports it, otherwise written in the prompt and validated).
- `llm` options: `max_output_tokens`, `prices`, `effort`, `fallbacks`, `base_url`, `api_key_env`.
- Dry runs estimate dollars when the price is known; runs report the real cost of the API backends.
- `doctor` checks each backend without spending anything (`--llm-check` makes one tiny call).
- README: start by opening the repository with Claude Code, which explains the tool and guides you.

## 0.1.1

- `init` and `migrate` commit everything they write in the instance repository, `.claude/` included
  (found by the first installation test: the Claude Code files were left uncommitted).

## 0.1.0

First version.

- **Cycle:** scan of `raw/` → extraction → chunks → quarantine of suspicious content → interpretation by
  the LLM (facts with a verified quote) → reconciliation per concept → SSOT in OKF format, written only by
  the curator, validated and committed in the git repository of the instance. Rebuildable from `raw/`,
  the decisions and the LLM cache.
- **Sources:** text and notes, PDF (pages without text read with OCR), Word, Excel/CSV (one location per
  row), images (OCR), mail (`.eml`, `.mbox`, Outlook `.msg`: one document per message, attachments as
  documents), WhatsApp exports (one location per message).
- **Quality:** quote verification on the sentence of the quote; evaluation set and `curator eval` with a
  baseline and a verdict; sources with many rejected facts listed for a check by hand.
- **Decisions of the owner:** approve, reject, resolve conflicts, merge, link, separate, rename, alias,
  refile a fact to another concept, release or discard from quarantine, supersede a source. Each decision
  carries an anchor and survives regenerations.
- **Exercise:** running by hand or periodically (`curator schedule` prints the command, nothing is
  installed), backup of the whole instance with a restore test, `curator forget` for privacy or legal
  deletion (hashes only).
- **Backends:** `claude-code` (subscription), `fake` (tests). Images read with local OCR (Tesseract) or not
  at all; a backend that sees images is not available yet.
- **Install:** `uv tool install`; `curator --version`; `curator migrate`; `doctor` checks the environment,
  the system dependencies and the version of the instance.
