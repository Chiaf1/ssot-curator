# Performance: known limits and plans

The curator is accurate but **slow on large volumes**: every block of every document is one LLM call, made
one after the other, and every call costs about 15,000 tokens. With a few dozen documents this does not
matter; with thousands, the first load takes many hours and a large share of a subscription's quota.

This page states the limits with measured numbers, explains what you can do today, and lists the
improvements under evaluation. **None of the improvements below is implemented yet**: the page will say
when one is, and in which version.

Measured on version 0.4.1 (0.4.2 has the same code). Last updated: 2026-10-23.

- [In short](#in-short)
- [Where the time and the tokens go](#where-the-time-and-the-tokens-go)
- [Known limits](#known-limits)
- [Estimating a large load](#estimating-a-large-load)
- [What you can do today](#what-you-can-do-today)
- [Improvements under evaluation](#improvements-under-evaluation)
- [How the numbers were measured](#how-the-numbers-were-measured)

## In short

- **Time ≈ number of LLM calls × 30–45 seconds.** The calls are sequential. 1,000 small documents need
  about 1,400 calls: **11 to 17 hours** with the `claude-code` backend.
- **Tokens ≈ 13,000–16,500 per call**, most of them overhead: for about 3,000 tokens of document text, a
  call sends about 7,000 tokens of fixed instructions and gets back about 5,600 tokens of answer.
  1,000 documents ≈ **13 million tokens**, about **30 runs** with the default limit of 400,000 per run.
  On a subscription, **the quota is probably the real limit**, before the time.
- **The local work grows linearly** (about 0.2 s per call) and has no quadratic bottleneck up to 2,000
  documents. But every run regenerates and re-validates the whole SSOT: **about 1.5 minutes per run at 2,000
  documents**, even when nothing changed.
- **After the first load it is cheap**: a document already read is never sent again (LLM cache). Adding a
  few documents costs only those documents, plus the fixed local time above.

## Where the time and the tokens go

### One reading call (measured on real documents)

From the 112 reading calls of a real test instance (about 30 documents of technical support: reports,
notes, manuals, spreadsheets), `claude-code` backend with Claude Opus:

| Part | Tokens per call | Note |
|---|---:|---|
| Claude Code's own input | ~5,000 | added by Claude Code to every call, whatever the prompt |
| Curator instructions (system prompt) | ~2,000 | the same for every call |
| Document text | ≤ 3,000 | `chunking.max_tokens`, default 3,000 |
| **Input, total** | **~10,000** | about 70% fixed overhead |
| **Output** (the facts, as JSON) | **~5,600** on average, up to 19,300 | about twice the text read |

About 60% of the output is structure rather than content:
- the concept and its key are repeated for every fact;
- optional fields are often empty;
- each fact carries both a text and a quote.

Output tokens are the slowest and most expensive part of a call.

Over 141 real calls (reading and reconciliation): **32.5 seconds per call on average**, 29–47 seconds in
the large runs, local time included.

### The local work (measured on a synthetic corpus, no LLM)

| Documents | Calls | Facts | First run | Run with nothing new | Run with +1% documents |
|---:|---:|---:|---:|---:|---:|
| 200 | 264 | 6,630 | 56 s | 16 s | 21 s |
| 1,000 | 1,373 | 33,527 | 258 s | 51 s | 71 s |
| 2,000 | 2,735 | 67,616 | 499 s | 86 s | 118 s |

At 2,000 documents, of the first run:
- 268 s go to validating the LLM answers against their schema (about 0.1 s per answer);
- 70 s go to verifying the quotes (about 1 ms per fact);
- 44 s go to regenerating every page;
- 31 s go to re-validating every page;
- 28 s go to rebuilding the search index.

The last three are paid **at every run and at every decision**, however small the change.

## Known limits

1. **Sequential calls.** One call at a time; each starts a new Claude Code process. Wall time is the sum of
   all calls.
2. **Fixed overhead per call.** About 7,000 input tokens per call do not depend on the document (5,000 from
   Claude Code, 2,000 of instructions); with blocks of at most 3,000 tokens, most of the input is overhead.
3. **Verbose answers.** The model writes about twice what it reads; a large part is repeated structure.
4. **Whole SSOT rebuilt at every run and decision.** Pages, validation and search index are regenerated from
   scratch: about 1.5 minutes at 2,000 documents, also for one `curator approve`.
5. **Answer validation is slow** (about 0.1 s per call): small next to the LLM, but more than half of the
   local time.
6. **Reconciliation re-sends a concept's whole history.** When a concept gets a new fact, all the groups of
   facts of that concept are judged again, with their texts and quotes. An attribute that changed many
   times over the years is re-sent in full at every change: the cost grows faster than the number of
   facts.
7. **Repeated content is read again.** The same text in two documents (two revisions of a manual, a mail
   quoted in another) costs two calls. Large tables go to the LLM 60 rows at a time.
8. **The token limit counts cached prompt tokens in full.** With `anthropic-api`, the system prompt read from
   the prompt cache costs a tenth, but `--max-tokens` counts it in full, so a run stops earlier than needed.
9. **Images and scans** cost one OCR pass each (seconds per page) when `images.reader: ocr` is on; not
   measured at scale.

## Estimating a large load

Before putting a large folder in `raw/`:

1. `curator run --dry-run` (0 tokens) gives the number of blocks and an estimate of the tokens. The estimate
   is usually 20–25% below the real use.
2. **Calls** ≈ blocks + reconciliation (in the synthetic benchmark, reconciliation added about 20%).
3. **Time** ≈ calls × 30–45 s, plus the local time of the table above.
4. **Runs** ≈ total tokens / `llm.max_tokens_per_run` (default 400,000). With a subscription, check how many
   your usage window allows.

The synthetic documents of the benchmark are small (1.15 blocks each); real reports and manuals have more
blocks per document, and the dry run is what counts.

## What you can do today

All of this works with the current version, without changes to the code:

- **Read only what is needed.** Exclude files that carry no facts (`ignore:` in `curator.yaml`). For long manuals, use
  `pages:` to read only the useful pages. Use `extract:` to let a folder give only some concept types (a
  smaller answer).
- **Split the load.** Put the documents in `raw/` in batches, most useful first, and run with
  `--max-tokens`: what is read stays read, and the SSOT is usable from the first batch.
- **Use `anthropic-api` for a very large first load.** It has no Claude Code overhead (about 5,000 tokens
  less per call) and the instructions go through the prompt cache. It is paid per token instead of
  subscription quota: estimate it with the dry run.
- **Try larger blocks, measuring the quality.** `chunking.max_tokens` (default 3,000) spreads the fixed
  overhead over more text: fewer calls, fewer tokens. Larger blocks may give fewer or worse facts: compare
  with `curator eval` before keeping it.
- **Avoid needless `rebuild`.** Changing a hint, a prompt or the model makes `rebuild` read everything again;
  for one folder, `curator run` after the change reads only what is new.

## Improvements under evaluation

Not implemented. Each needs a measurement and, where it changes the rules or the recipe, the owner's
decision. The order is the one currently planned.

| # | Improvement | Expected effect | Cost and risk |
|---|---|---|---|
| 1 | **Measure the per-call time in every run report** | the numbers above for every instance | none |
| 2 | **Cache the validation of the answers** | about half of the local time | low |
| 3 | **Calls in parallel** (configurable, e.g. 3–6) with the token budget reserved per call | first load and `rebuild` 3–5× faster; same tokens | medium: order of results kept deterministic; limited by the subscription or API rate limits, and by the quota |
| 4 | **Larger blocks**, after measuring with `curator eval` | input tokens −40–50%, about a third of the calls | quality of extraction on long blocks; longer answers |
| 5 | **Compact answers** (facts grouped by concept, empty fields omitted, shorter quotes) | output tokens −30–40% | a new prompt version; an instance switches with one `rebuild` |
| 6 | **A smaller model for reading** (e.g. Sonnet), Opus for reconciliation | faster and cheaper calls | quality: to measure with `curator eval` |
| 7 | **Incremental reconciliation** (cache per group of facts; new facts compared with the current values, not the whole history) | reconciliation tokens stop growing with the history | medium |
| 8 | **Incremental pages, validation and search index** (only the concepts touched) | run and decision time independent of the SSOT size | larger change; the output must stay identical |
| 9 | **Skip what carries no facts** (tables of contents, boilerplate) and **read repeated blocks once** | fewer calls on manuals and mail | must never skip real content |
| 10 | **Batch API** for `anthropic-api` | half the cost of a large load | results arrive later (asynchronous) |
| 11 | **Count cached prompt tokens at their real weight** in the token limit | runs do not stop early | low |

A rough target for 3 + 4 + 5 together: a call from about 15,500 to 7,000–9,000 tokens, and the total time
of a first load divided by 5–10. To be confirmed by measurement.

## How the numbers were measured

- **Real calls**: the run reports of a real test instance (`state/runs/*.json`) and its LLM cache, read with
  `tools/benchmark/run_times.py <instance>` (reads only, 0 tokens).
- **Local work**: `tools/benchmark/bench_run.py <empty folder> <documents>` builds a synthetic corpus
  (Word, PDF, text notes, spreadsheets), answers with a synthetic backend instead of the LLM (no tokens) and
  times every stage of three runs: first, nothing new, +1% documents. Measured on Linux, 4 cores, Python
  3.13, without OCR. Local times depend on the machine: the first run of 50 documents took 16 s there and
  60 s on a Windows laptop (same calls, same facts).

```bash
CURATOR_NOW=2026-10-22T09:00:00+02:00 python -I tools/benchmark/bench_run.py /tmp/bench 500
python tools/benchmark/run_times.py path/to/instance
```
