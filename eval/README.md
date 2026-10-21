# Evaluation set (template)

Synthetic evaluation set for the examples in `examples/` (`curator eval`, recipe §16). An instance keeps its own
set in `eval/` (with real data it stays outside git and inside the backup).

- `cases/*.yaml`: interpretation cases. A case points to a REAL chunk of a file in `raw/` (`source`, `chunk` or
  `anchors`) and lists the facts that must be extracted (`expect`) and those that must not (`absent`).
  `complete: true` means the list is complete: only those cases count for precision.
- `reconcile/*.yaml`: reconciliation cases (groups of facts written by hand, with the expected relation).
- `expectations.yaml`: global expectations, e.g. the number of distinct concepts of a type.

Matching of an expected fact: same type (`type` can be a list: the information matters, not where the
LLM files it); same concept (`key` or `path`) and `field` when given; every word in
`contains` (and at least one in `any`) present in the fact text or quote; `date` equal to the verified date.
`path` can be a glob (`axes/bl2/axis{1,2,3}`).

A forbidden fact (`absent`) is either text (`contains`: every word present) or, with `type`, matched like an
expected fact: `{type: Axis, path: "axes/*"}` flags any fact filed under a concept that must not exist
(e.g. identifiers invented from the numbering of a list). A case can use a source excluded from the SSOT
with an `ignore` rule: evaluating it is still useful to see whether a prompt repeats a known mistake.

Trying a change on part of the set before paying for all of it: `curator eval --prompt interpret-v4
--source "service-reports/*" --baseline baseline-docs` compares with `eval/baseline-docs.json`, saved before with
the prompt in use on the same `--source` (from the cache: free). `--source` and `--case ID` are repeatable; the reconciliation
cases always run (they do not depend on the interpretation prompt).
