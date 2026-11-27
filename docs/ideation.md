# Tournament ideation

The default backend, `sakana-scientist-v3`, keeps the existing single-idea workflow.
The optional `coscientist` backend generates literature-grounded candidates, reviews
them, ranks them through Elo matches, and evolves mature candidates. Only the best
eligible candidate runs. Predictions and falsifiers are mandatory; measured outcomes
feed subsequent generator and judge prompts.

Enable it through the pipeline:

```bash
agsci run examples/circpack execution.phases=investigation \
  investigation.ideation.backend=coscientist \
  'investigation.ideation.literature.sources=["folder:/path/to/papers"]'
```

For `agsci investigate`, omit the `investigation.` prefix on overrides.

## Literature sources

Pass a list of sources. Options follow `|`; unknown options fail validation.

| Source | Options | Example |
|---|---|---|
| `folder:<path>` | none | `folder:/path/to/papers` |
| `arxiv:<query>` | `max=50`, `from=YYYY-MM-DD`, `cat=cs.CV,cs.LG` | `arxiv:diffusion policy|max=20` |
| `<venue>:<years>:<query>` | `max=100` | `iclr:2025,2026:diffusion polic|max=40` |

Venues are `iclr`, `cvpr`, `neurips` (alias `nips`), and `icml`. Conference
matching requires every query fragment in the title or abstract. Truncated dataset
rows are skipped. HTTP 429 responses receive five exponential-backoff retries.
arXiv uses pages of at most 100 and waits at least three seconds between pages.

Folders are searched recursively. `records.json` takes priority over Markdown and
BibTeX. Markdown uses the first heading as the title and the `## Abstract` section
as the abstract. BibTeX reads title, abstract, authors, year, and URL when present.
PDFs are not parsed; a PDF-only folder produces an explicit error.

An example collection from the development environment is:

```text
/home/s224075134/scratch/agentic/scientist-agent/sputnik/outputs/literature/conference_collections/20261123-075113-diffusion-policy-2025-2026
```

Build a reusable corpus on the host:

```bash
agsci literature build \
  --source 'folder:/path/to/papers' \
  --source 'arxiv:diffusion policy|max=20' \
  --out /path/to/corpus
```

Then set `investigation.ideation.literature.corpus_dir=/path/to/corpus`. This skips
fetching. Without that override the parent builds `<results_dir>/literature` once;
parallel workers reuse it and keep separate candidate pools. An existing corpus
with identical sources is never rewritten; changing sources requires a new directory.
Checksums detect corpus modification. Partial source failures are recorded in the
manifest; an entirely empty corpus fails by default. `--allow-empty` permits empty
CLI builds. Role prompts contain bounded literature packs and accept only supplied
pack citation ids (`L` plus eight hex digits). Corpus fetching happens on the host.
The literature restriction is a prompt policy, matching the existing idea runner.
The shared launcher grants broad agent tools; it does not enforce provider-only
egress. Container `network=none` blocks hosted model APIs too, so use an external
provider-aware egress policy if hard isolation is required. Roles use fresh
container sessions, each mounting only its own workspace.

## Configuration

All keys below are under `investigation.ideation` in pipeline commands.

| Key | Default | Purpose |
|---|---|---|
| `backend` | `sakana-scientist-v3` | Select `coscientist` for tournaments |
| `literature.sources` | `[]` | Source specs; required unless a corpus directory is supplied |
| `literature.corpus_dir` | `null` | Frozen, prebuilt corpus directory |
| `literature.require_nonempty` | `true` | Reject an empty corpus |
| `judge.agent` | `null` | Inherit the current generator agent |
| `judge.model` | `null` | Inherit its model |
| `judge.reasoning_effort` | `null` | Inherit its reasoning effort |
| `per_round` | `3` | New generation calls every cycle, even when the pool is full |
| `matches_per_round` | `6` | Pairwise comparisons |
| `evolve_min_mature` | `4` | Mature candidates required to evolve |
| `evolve_mature_matches` | `2` | Matches required for maturity |
| `evolve_top_k` | `4` | Maximum evolution parents |
| `evolve_strategies` | `[combine,simplify,out_of_box]` | Also supports `feasibility` |
| `metareview_every` | `3` | Meta-review cadence; zero disables it |
| `pack_k_generate` | `8` | Generation literature records |
| `pack_k_reflect` | `6` | Other roles' literature records |
| `dedup_threshold` | `0.85` | TF-IDF cosine duplicate threshold |
| `max_pool` | `40` | Eligible pool cap; excess candidates retire |
| `role_timeout_seconds` | `1800` | Timeout per agent invocation |

Prefer a judge with a different agent or model from the generator. Reflection and
ranking use that judge. Identical agent/model pairs log
`ideation_judge_not_independent` once per run without stopping execution.
The coscientist backend forces `execution.require_preregistration=true`.

## Outputs and recovery

```text
<results_dir>/
  literature/
    corpus.jsonl
    manifest.json
  ideation/
    candidates.jsonl
    reviews.jsonl
    matches.jsonl
    outcomes.jsonl
    feedback.jsonl
    rounds/0001/<step>/
      prompt.md
      candidate.json | review.json | verdict.json | feedback.json
      completed.json
  prediction_ledger.jsonl
```

Candidate status and selection events are append-only. Elo starts at 1200 and is
replayed from unique `(cycle, match_index)` matches. K is 32 until both candidates
have five matches, then 16. Invalid judge output records a match without changing
Elo. Failed generation reduces the candidate count. Completed role calls are cached,
including failed calls. Resuming a round skips those calls and recorded matches.
Evolved children enter competition in the next cycle. Deduplication includes
executed candidates and historical titles. A rejected artifact marks the selected
candidate invalid and retries with the next eligible candidate. If none remain,
normal campaign generation-failure handling applies.

The selected bundle uses the usual `idea_bundle.json` and `target_rubrics.json`.
Executed candidates leave the selectable pool. Outcomes retain selection-time Elo,
measured score and baseline, pass status, and falsifier outcome. Spearman calibration
compares Elo with measured score delta when at least three usable outcomes exist;
constant ranks have no defined correlation.

## Estimated and measured cost

Without evolution, the default upper bound is `2*per_round + matches_per_round`,
or **12 role calls per cycle**. Each evolution strategy adds a generation and a
reflection: three strategies bring this to **18 calls**. Meta-review adds one on
its scheduled cycles. Duplicates, failed generation, or too few eligible candidates
reduce that count. Attempt execution is additional. Each call has its own prompt,
output and agent log, and uses the existing headless cost recorder.

No live corpus build or real `agsci` smoke run was performed for this implementation.
These are estimates, with no measured wall time or billing result. A small manual
smoke command is:

```bash
agsci run examples/circpack execution.phases=investigation \
  investigation.execution.max_idea_cycles=2 \
  investigation.ideation.backend=coscientist \
  investigation.ideation.per_round=2 investigation.ideation.matches_per_round=2 \
  'investigation.ideation.literature.sources=["arxiv:circle packing|max=10"]'
```

Inspect `ideation/outcomes.jsonl` and `prediction_ledger.jsonl` for matching
`cand_` idea ids after running it. Candidate and match counts depend on generation,
review acceptance, and duplication; they are not guaranteed by the cycle count.

## Rank proposals before spending experiment compute

`agsci propose run` ranks research ideas on paper using the frozen literature
corpus, full reviews, assumption verification, and Elo debates. It runs native
role sessions and no experiments. Prompts instruct roles to keep an optional
codebase read-only and avoid network access; these are prompt policies rather
than filesystem or egress enforcement.

```bash
agsci propose run --goal 'Reduce error accumulation in video diffusion' \
  --codebase /path/to/model --source 'folder:/path/to/papers' \
  --agent codex --model MODEL --judge-agent claude \
  --rounds 4 --per-round 3 --matches-per-round 6 --out results/my-proposals
```

Use `--goal-file goal.txt` instead of `--goal` for a longer brief. Without `--out`,
runs go to `results/proposals/<goal-slug>/<YYYYMMDD_HHMMSS>/`. Corpus building
uses the source grammar above, including network sources when explicitly given;
an existing `<out>/literature` is reused. Role calls use only that frozen corpus.
Rerun the same command and output directory to resume. Changed goal, codebase,
settings or agents require a new directory.

Outputs include `plan.json`, optional `codebase.json`, `ideation/` pool logs and
Elo snapshots, ranked `proposals.jsonl`, `overview.md`, and `status.json`.
The default stop is four rounds or two consecutive snapshots with the same
unordered top-five candidate ids. Fewer than two eligible candidates stops with
`state=no_candidates`; any eligible proposal remains in the outputs for review.
Elo ranks paper plausibility, not measured experimental performance. Unknown
candidate and literature references are stripped from the overview.

Select proposals explicitly, in the order they should be executed:

```bash
agsci propose select results/my-proposals cand_FIRST cand_SECOND
agsci run /path/to/task investigation.ideation.backend=coscientist \
  investigation.ideation.literature.corpus_dir=results/my-proposals/literature \
  investigation.ideation.seed_proposals=results/my-proposals/selected.jsonl
```

`select` writes `selected.jsonl` and rejects unknown or duplicate ids. The
coscientist execution backend concretizes each selected proposal against
`task.md`, producing the normal implementation plan, rubrics, prediction and
numeric falsifier, then reflects it and selects it before ordinary Elo-ranked
candidates. Conversion and reflection resume from cached steps. Once the seeds
are consumed, normal tournament rounds resume. The default backend does not
accept `seed_proposals`; its default is `null`.

## Minimal Codex homes for role calls

Native Codex roles (including proposal and research-discovery roles) use a fresh,
private home under `$HOME/.cache/agsci-role-home/` for each invocation. This avoids
loading the interactive home's skills, plugins, MCP servers and hooks. Model and
provider configuration, HTTP settings, and supported sandbox/environment policies
are retained; a selected profile's supported settings are flattened into the role
configuration. Unknown custom configuration keys are not copied. Workspace-level
instructions are not disabled by this change.

Before every call, `auth.json` is copied from `${CODEX_HOME:-$HOME/.codex}` into the
new home. Both `HOME` and `CODEX_HOME` point at that isolated copy during the call.
Directories are owner-only (0700), copied files are 0600, parallel calls do not
share credentials, and the existing npm cache is reused. No user config or auth
file is overwritten. Unchanged homes are removed on success, failure and handled
interrupts; SIGKILL cannot trigger cleanup.

If Codex refreshes credentials, the private role home is retained and a warning
prints its path. The refreshed credentials are **not** automatically written back
or reused by subsequent calls: those still copy the current user credentials.
For accounts requiring rotating refresh tokens across long runs, opt out until
credential synchronization is explicitly enabled through a separate workflow.
File-backed auth and inherited API-key environment variables are supported;
keyring-only setups and unusual configs may need the opt-out as well.

- `AGSCI_ROLE_HOME=/private/path` selects the parent directory for fresh role homes.
- `AGSCI_ROLE_HOME=off` preserves the normal interactive home and config.
- Other agent backends and containerized role launches are unchanged.
- The Python runner selects its own interpreter for shell helpers by default;
  an explicit `WRAPPER_PYTHON` override is still honored (Python 3.11+ is required).

A previous tiny-call comparison measured 3,797 fewer input tokens with a minimal
home. That is not an end-to-end savings or scientific-quality benchmark of this
integration.
