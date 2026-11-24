# Tournament Ideation (Co-Scientist Adaptation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the "one idea per cycle" step of the investigation loop with an optional Co-Scientist-style round. The round has four stages: generate candidates grounded in a user-chosen literature corpus, review them, rank them in an Elo tournament, and evolve them. Only the top candidate is executed, and the measured result is fed back to the pool.

**Architecture:** A new package `src/ideation/` owns two things: a frozen, per-run literature corpus (built on the host from `folder:`, `arxiv:` and `iclr|cvpr|neurips|icml:` sources) and a persistent candidate pool under `results/<run>/ideation/`. The pool is stored as JSONL, and Elo is replayed from `matches.jsonl`. Ideation is pluggable: `investigation.ideation.backend` names an `IdeationBackend` in a small registry (`src/ideation/backends.py`). `sakana-scientist-v3` (the default) wraps today's single-idea `run_idea_generation` unchanged. `coscientist` is the tournament runner. `main_entry` asks the registry for the backend's runner and outcome hook, and passes them to `campaign_loop`. Every backend has the same `run_idea_generation` keyword signature. The coscientist backend writes the selected candidate as the usual `idea_bundle.json` and `target_rubrics.json`, so `campaign_loop` validation, commitment, attempt and ledger stay unchanged. One new hook reports each attempt outcome back to the pool. Every role (generate, reflect, judge, evolve, meta-review) is one headless-agent call through a slim `src/scripts/ideation_role.sh`. The call gets a fully rendered prompt and returns exactly one JSON file.

**Tech Stack:** Python ≥3.11, standard library only (`urllib.request`, `xml.etree.ElementTree`, `json`, `math`, `hashlib`, `random`), and the existing Hydra config. Role agents launch through the existing `src/scripts/lib/container_agent.sh` and `run_agent_prompt`. Tests use pytest.

**Spec:** There is no separate spec document. This plan argues from the session discussion, summarized in "Design decisions" below. Reference implementation: `/home/s224075134/scratch/agentic/scientist-agent/Co-Scientist` (Apache-2.0). Example folder corpus: `/home/s224075134/scratch/agentic/scientist-agent/sputnik/outputs/literature/conference_collections/20261123-075113-diffusion-policy-2025-2026`.

## Design decisions

1. **Same seam, same artifacts.** The tournament runner implements the `run_idea_generation_fn` keyword signature and returns `IdeaGenerationResult(exit_code=0, idea_dir=output_dir)`. The existing retry loop in `campaign_loop.py` re-invokes it with `validation_feedback` when `load_idea_artifacts` rejects the selected candidate. The runner then marks that candidate `invalid` and selects the next one.
2. **Literature is fetched by the host, once, and frozen.** Role agents never get network access. They see only a rendered "literature pack" inside their prompt, and every citation must be a pack id (`L` plus 8 hex digits). This mirrors Co-Scientist's seen-URL filter and keeps v3's no-network idea-agent rule.
3. **Reality beats Elo.** An executed candidate leaves the selectable pool and its measured result is recorded. The Spearman correlation between Elo at selection time and the measured delta is shown to the judge and generator prompts and to meta-review, so tournament taste is scored against outcomes. This is the v3 addition Co-Scientist lacks.
4. **Pre-registration is mandatory for the coscientist backend.** Each candidate must carry `prediction` and `falsifier`, the fields from the `feat/idea-preregistration-ledger` branch. Reality anchoring relies on them.
5. **Independent judge.** Reflection and ranking use `investigation.ideation.judge.agent` and `.model`. If these equal the generator's agent and model, log `ideation_judge_not_independent` once per run. Don't fail the run.
6. **No shared writers.** Each worker owns its pool under its own `results_dir`. The corpus is built once by the parent and is read-only.

## Global Constraints

- Backend names are exactly `sakana-scientist-v3` and `coscientist`. An unknown name raises an error listing the registered names. The default is `investigation.ideation.backend: sakana-scientist-v3`. With the default, behaviour is unchanged and every existing test passes. The effective-config golden fixture changes only by the new keys.
- No new runtime dependencies. Python ≥3.11, standard library only.
- Supported venues: `iclr`, `cvpr`, `neurips`, `icml`. The dataset id is `ai-conferences/{ICLR|CVPR|NeurIPS|ICML}{year}` on `https://datasets-server.huggingface.co`.
- arXiv endpoint: `http://export.arxiv.org/api/query`. Wait ≥3 s between requests and use at most 100 results per page.
- The User-Agent for every outbound request is exactly `agentic-scientist-literature/0.1`. Never add an email or the user's name.
- The corpus lives at `<results_dir>/literature/{corpus.jsonl,manifest.json}`. Once a manifest exists with identical `sources`, it is never rewritten.
- Pool state lives at `<results_dir>/ideation/{candidates.jsonl,reviews.jsonl,matches.jsonl,outcomes.jsonl,feedback.jsonl,rounds/<cycle:04d>/}`. All files are append-only.
- Elo: initial 1200; K=32 while the less-experienced player has <5 matches, else K=16; expected score `1/(1+10^((b-a)/400))`.
- Candidate and literature ids are deterministic. Candidate id is `cand_` plus the first 10 hex digits of sha1 of `title + "\n" + hypothesis`. Literature id is `L` plus the first 8 hex digits of sha1 of the URL, or of the lowercased title when the record has no URL.
- Prompts adapted from Co-Scientist `config/prompts/` keep a header line `<!-- Adapted from Co-Scientist (Apache-2.0), config/prompts/<file> -->`.
- Run all tests with `env -u HEADLESS_BILLING`, because that variable breaks `tests/runtime/test_container_agent.py` on this host.

## Review corrections (2026-11-24)

- Pass `cycle_index` to `normalize_idea_bundle`.
- Prepare the parent corpus before the parallel branch and forward its absolute directory.
- Adapt ledger `cycle_index` and attempt pass status explicitly to the outcome hook.
- Persist role completion (including failed output), selection-time Elo, and round status so retries and crash recovery do not repeat calls or reset executed candidates.
- Keep evolved children ineligible until the next cycle.
- Task 9: skip both live commands; document estimated costs as unmeasured. Count evolution reflections too: defaults are 12 calls without evolution, 18 with all three strategies, plus scheduled meta-review.

## Review Focus

1. **HF rate limits (429).** The example sputnik manifest shows that 7 of 8 venue crawls failed with 429. Retry up to 5 times with backoff (2 s doubling, 60 s cap), then record a per-source error in `manifest.json` and continue. Fail the build only when the whole corpus is empty and `literature.require_nonempty` is true; the error message must name every failed source. Tests: `test_conference_retries_429_then_records_error` and `test_build_fails_when_all_sources_empty` (Task 2).
2. **Resume after a crash mid-round.** Elo is recomputed by replaying `matches.jsonl`. A match is idempotent by `(cycle, match_index)`, and a rerun of the same cycle must not repeat completed role calls. Tests: `test_elo_replay_is_idempotent` (Task 4) and `test_round_resume_skips_completed_matches` (Task 7).
3. **Folder layouts other than sputnik.** Handle a folder with only `*.md`, nested directories, a `references.bib` only, or only PDFs. The first three read; a PDFs-only folder raises `LiteratureSourceError` naming the folder and saying PDFs are not parsed. Tests: `test_folder_markdown_only`, `test_folder_bib_only`, and `test_folder_pdfs_only_errors` (Task 1).
4. **Malformed role output.** A missing or invalid judge JSON gives a match recorded as `invalid` with no Elo change. A failed generation means fewer candidates. If the round ends with no eligible candidate, the runner returns `exit_code=1` with output `ideation_no_eligible_candidate`, so the existing retry and failure path handles it. Tests: `test_invalid_judge_output_no_elo_change` (Task 6) and `test_round_with_no_eligible_candidate_fails_cleanly` (Task 7).
5. **Pool exhaustion and duplicates of executed ideas.** When fewer than `generation.per_round` eligible candidates remain, the round still generates new ones. Dedup compares against all candidates, including executed ones, and against `ideas_history.jsonl` titles. Tests: `test_dedup_against_executed_and_history` (Task 3) and `test_round_generates_when_pool_exhausted` (Task 7).

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/ideation/__init__.py` | package marker |
| `src/ideation/literature/sources.py` | `SourceSpec` parsing; `LiteratureRecord`; fetchers `fetch_folder`, `fetch_arxiv`, `fetch_conference` |
| `src/ideation/literature/corpus.py` | `build_corpus`, `load_corpus`, manifest and hashing; `agsci literature build` entry |
| `src/ideation/literature/retrieval.py` | BM25 index over title+abstract; `render_pack` |
| `src/ideation/similarity.py` | TF-IDF cosine used for dedup and pairing distance |
| `src/ideation/pool.py` | `Candidate`, `Review`, `Match`, `Outcome` records; JSONL persistence; `PoolState` replay |
| `src/ideation/elo.py` | Elo math, pair selection, judge-mode selection |
| `src/ideation/roles.py` | per-role prompt rendering (from `src/prompts/ideation/*.md`) and output parsing/validation |
| `src/ideation/role_runner.py` | `run_role(...)`: launches `ideation_role.sh`, returns parsed JSON or `None` |
| `src/ideation/round.py` | `run_tournament_round(...)` plus `make_tournament_idea_generation(...)` and `record_outcome` |
| `src/ideation/backends.py` | `IdeationBackend` protocol, the registry, and the two backends `sakana-scientist-v3` and `coscientist` |
| `src/prompts/ideation/{generate,reflect,judge_pairwise,judge_debate,evolve_combine,evolve_simplify,evolve_out_of_box,evolve_feasibility,metareview}.md` | role prompts |
| `src/scripts/ideation_role.sh` | slim headless-agent launcher: prompt file in, one JSON file out |
| `src/investigation/config.py`, `config_loader.py`, `configs/config.yaml`, `parallel_helpers.py` | `IdeationConfig` surface and worker forwarding |
| `src/investigation/main_entry.py`, `campaign_loop.py` | seam wiring and outcome hook |
| `src/agsci/main.py` | `agsci literature build` subcommand |
| `src/agents/skills/create-idea/SKILL.md` | add `prediction` and `falsifier` (outstanding fix from the ledger branch) |
| `docs/ideation.md` | user docs: source grammar, config keys, cost, outputs |
| `tests/ideation/…` | one test module per source file above |

---

### Task 0: Branch and the outstanding skill fix

**Files:**
- Modify: `src/agents/skills/create-idea/SKILL.md` (key list under "What to do", step 4)
- Test: `tests/investigation/test_campaign_ideas.py`

**Interfaces:** none

- [ ] **Step 1:** `git checkout feat/idea-preregistration-ledger && git checkout -b feat/tournament-ideation`
- [ ] **Step 2: Write the failing test** `test_create_idea_skill_lists_preregistration_fields`. It reads the SKILL.md and asserts that `` `prediction` `` and `` `falsifier` `` both appear in step 4's key list.
- [ ] **Step 3:** Run `env -u HEADLESS_BILLING python -m pytest tests/investigation/test_campaign_ideas.py -k skill_lists -q`. Expected: FAIL.
- [ ] **Step 4:** Add the two keys to SKILL.md step 4, with the same one-line definitions as `src/prompts/idea.md`.
- [ ] **Step 5:** Run the test. Expected: PASS. Commit: `fix(create-idea): list prediction and falsifier in the skill contract`.

---

### Task 1: Source specs and fetchers

**Files:**
- Create: `src/ideation/__init__.py`, `src/ideation/literature/__init__.py`, `src/ideation/literature/sources.py`
- Test: `tests/ideation/test_sources.py`, plus fixtures under `tests/ideation/fixtures/`. Fixtures: `arxiv_feed.xml` (2 entries), `hf_rows.json` (3 rows, one with `truncated_cells`), and a `sputnik_folder/` containing `records.json` (2 records copied from the example folder), one `.md` and one `.bib`.

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class SourceSpec: kind: Literal["folder","arxiv","conference"]; raw: str; path: Path | None; query: str; venue: str | None; years: tuple[int, ...]; max_results: int; date_from: date | None; categories: tuple[str, ...]`
  - `parse_source_spec(text: str) -> SourceSpec`
  - `@dataclass(frozen=True) class LiteratureRecord: id: str; title: str; abstract: str; authors: tuple[str, ...]; year: int | None; venue: str; url: str; arxiv_id: str; source: str` (`source` is the spec's `raw`)
  - `fetch_folder(spec) -> list[LiteratureRecord]`
  - `fetch_arxiv(spec, *, http_get: Callable[[str], bytes], sleep: Callable[[float], None]) -> list[LiteratureRecord]`
  - `fetch_conference(spec, *, http_get: Callable[[str], bytes], sleep: Callable[[float], None]) -> list[LiteratureRecord]`
  - `class LiteratureSourceError(ValueError)`
  - `class HttpStatusError(OSError)` with attribute `status: int`

**Spec grammar.** Options follow `|` as `key=value`. Unknown keys raise.
- `folder:<path>`
- `arxiv:<query>`, with options `max=<int>` (default 50), `from=<YYYY-MM-DD>`, and `cat=<a,b>`
- `<venue>:<year>[,<year>…]:<query>`, where venue is `iclr|cvpr|neurips|icml` (also `nips`, an alias of neurips), with option `max=<int>` (default 100)

- [ ] **Step 1: Write the failing tests:**
  - `test_parse_specs`: `parse_source_spec("iclr:2025,2026:diffusion polic|max=40")` gives `kind="conference", venue="ICLR", years=(2025,2026), query="diffusion polic", max_results=40`. `"arxiv:video diffusion error accumulation|from=2025-01-01|cat=cs.CV,cs.LG"` gives `date_from=date(2025,1,1), categories=("cs.CV","cs.LG")`. `"folder:/x"` gives `path=Path("/x")`. `"cvpr:20x:q"`, `"arxiv:"` and `"bogus:q"` each raise `LiteratureSourceError`.
  - `test_folder_sputnik_records`: the fixture folder yields 2 records whose titles and URLs equal `records.json`. Markdown and bib are ignored when `records.json` exists.
  - `test_folder_markdown_only`: the first `# ` heading becomes the title and the `## Abstract` section becomes the abstract. Files are found recursively.
  - `test_folder_bib_only`: `title` and `abstract` (if present) are parsed from `@article{…}` entries.
  - `test_folder_pdfs_only_errors`: the error message contains the folder path and `"PDFs are not parsed"`.
  - `test_arxiv_parses_feed_and_filters_date`: with a fake `http_get` returning the fixture, records after `from` are kept, `arxiv_id` is set, and the requested URL contains `search_query=all:video+AND+all:diffusion` and `sortBy=submittedDate`. `sleep` is called with `3.0` between pages.
  - `test_conference_filters_fragments`: every query word fragment must appear in title+abstract, case-insensitively. The truncated row is skipped. The URL uses the `/search?` endpoint with `dataset=ai-conferences%2FICLR2025`.
  - `test_conference_retries_429_then_records_error`: a fake `http_get` raises `HttpStatusError(429)` 5 times. The function raises `LiteratureSourceError` mentioning `429`, after sleeps of `[2, 4, 8, 16, 32]`.
- [ ] **Step 2:** Run `env -u HEADLESS_BILLING python -m pytest tests/ideation/test_sources.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement `sources.py`.** The real `http_get` is `urllib.request` with the User-Agent from Global Constraints and a 30 s timeout. It raises `HttpStatusError` on non-200. Use `xml.etree.ElementTree` with the Atom namespace for arXiv. For conferences, page `/search` 100 rows at a time until `max_results` or `num_rows_total`.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit: `feat(ideation): literature source specs and fetchers`.

---

### Task 2: Frozen corpus and the `agsci literature build` command

**Files:**
- Create: `src/ideation/literature/corpus.py`
- Modify: `src/agsci/main.py` (add a `literature` command next to `review`)
- Test: `tests/ideation/test_corpus.py`

**Interfaces:**
- Consumes: Task 1's `parse_source_spec`, the `fetch_*` functions and `LiteratureRecord`.
- Produces:
  - `build_corpus(out_dir: Path, specs: Sequence[str], *, require_nonempty: bool, fetchers: Mapping[str, Callable[[SourceSpec], list[LiteratureRecord]]] | None = None) -> Path`. It returns the manifest path and dedupes by `id`, then by `arxiv_id`.
  - `load_corpus(out_dir: Path) -> list[LiteratureRecord]`
  - Manifest JSON: `{"schema_version": 1, "sources": [...raw specs], "built_at": iso, "counts": {raw: n}, "errors": {raw: msg}, "corpus_sha256": hex, "record_count": n}`
  - CLI: `agsci literature build --source <spec> [--source …] --out <dir> [--allow-empty]`. It prints the counts and errors.

- [ ] **Step 1: Write the failing tests:**
  - `test_build_writes_sorted_corpus_and_manifest`: records are sorted by `id`, and `corpus_sha256` matches the file.
  - `test_build_is_noop_when_sources_unchanged`: the file mtime is unchanged and the fetchers are not called.
  - `test_build_rejects_changed_sources_on_existing_corpus`: raises `LiteratureSourceError("corpus already built from different sources")`.
  - `test_partial_failure_records_error`: one fetcher raises, the other returns 2 records, so the manifest has `errors` with 1 entry and `record_count` 2.
  - `test_build_fails_when_all_sources_empty`: with `require_nonempty=True`, the error names each failed spec.
  - `test_cli_build_folder`: `main(["literature","build","--source",f"folder:{fixture}","--out",str(tmp)])` returns 0, and `corpus.jsonl` has 2 lines.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement `corpus.py`. Write files atomically (temp file, then `os.replace`). Add the CLI branch to `src/agsci/main.py`.
- [ ] **Step 4:** Run the tests. Expected: PASS. Commit: `feat(ideation): frozen literature corpus and agsci literature build`.

---

### Task 3: Retrieval pack and similarity

**Files:**
- Create: `src/ideation/literature/retrieval.py`, `src/ideation/similarity.py`
- Test: `tests/ideation/test_retrieval.py`, `tests/ideation/test_similarity.py`

**Interfaces:**
- Produces:
  - `class Bm25Index: __init__(self, records: Sequence[LiteratureRecord], *, k1: float = 1.5, b: float = 0.75); search(self, query: str, k: int) -> list[LiteratureRecord]`. Tokens are lowercase `[a-z0-9]+` with no stopword list.
  - `render_pack(records: Sequence[LiteratureRecord], *, max_abstract_chars: int = 1200) -> str`. Each entry is `### [L…] Title (Venue Year)\nURL\n\nabstract`.
  - `pack_ids(records) -> frozenset[str]`
  - `text_similarity(a: str, b: str, corpus: Sequence[str]) -> float`, a TF-IDF cosine in [0, 1].
  - `find_duplicate(text: str, existing: Mapping[str, str], *, threshold: float) -> str | None`, which returns the key of the most similar entry at or above the threshold.

- [ ] **Step 1: Write the failing tests:**
  - `test_bm25_ranks_matching_abstract_first`
  - `test_bm25_empty_query_returns_empty`
  - `test_render_pack_truncates_and_labels`
  - `test_similarity_identical_is_one_disjoint_is_zero`
  - `test_dedup_against_executed_and_history`: `existing` mixes candidate texts and history titles, and a near-paraphrase with threshold 0.85 returns the right key.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement both modules.
- [ ] **Step 4:** Run them. Expected: PASS. Commit: `feat(ideation): bm25 literature packs and tf-idf dedup`.

---

### Task 4: Pool records, persistence and Elo replay

**Files:**
- Create: `src/ideation/pool.py`, `src/ideation/elo.py`
- Test: `tests/ideation/test_pool.py`, `tests/ideation/test_elo.py`

**Interfaces:**
- Consumes: `src.orchestrator.state.append_jsonl`
- Produces (`pool.py`):
  - `CandidateStatus = Literal["draft","rejected","eligible","executed","invalid","retired"]`
  - `@dataclass class Candidate: id: str; cycle: int; strategy: str; parent_ids: tuple[str, ...]; bundle: dict[str, Any]; citations: tuple[str, ...]; rationale: str`. Here `bundle` is the normalized idea bundle, including prediction and falsifier.
  - `@dataclass class Review: candidate_id: str; verdict: Literal["pass","neutral","already_explained","already_tried","not_implementable","disproved"]; summary: str; citations: tuple[str, ...]; rejected_citations: int`
  - `@dataclass class Match: cycle: int; match_index: int; a: str; b: str; mode: Literal["pairwise","debate"]; winner: Literal["a","b"] | None; rationale: str`, where `winner=None` means invalid.
  - `@dataclass class Outcome: candidate_id: str; cycle: int; attempt_id: str; elo_at_selection: float; combined_score: float | None; baseline_score: float | None; attempt_pass: bool; falsifier_outcome: str`
  - `class PoolStore: __init__(self, root: Path)` with `add_candidate`, `add_review`, `add_match`, `add_outcome`, `add_feedback(dict)`, `mark(candidate_id, status, reason)`, and `load() -> PoolState`. Writes are append-only. `add_match` is a no-op if `(cycle, match_index)` already exists.
  - `@dataclass class PoolState: candidates: dict[str, Candidate]; status: dict[str, CandidateStatus]; elo: dict[str, float]; matches_played: dict[str, int]; reviews: dict[str, Review]; outcomes: list[Outcome]; feedback: list[dict]`
  - `PoolState.eligible() -> list[Candidate]`, sorted by Elo descending, then id.
- Produces (`elo.py`):
  - `expected_score(a: float, b: float) -> float`
  - `update(elo_a: float, elo_b: float, winner: Literal["a","b"], min_played: int) -> tuple[float, float]`
  - `select_pair(eligible: Sequence[Candidate], state: PoolState, rng: random.Random, similarity: Callable[[str, str], float]) -> tuple[str, str] | None`
  - `select_mode(state: PoolState, a: str, b: str) -> Literal["pairwise","debate"]`
  - `reality_calibration(outcomes: Sequence[Outcome]) -> dict[str, float | int | None]`, which returns `{"n": int, "spearman": float | None}`. It uses outcomes with both scores, and `None` when n < 3.

**Pair selection (from Co-Scientist `agents/ranking.py`).** Draw `r = rng.random()`. "New" means fewer than 3 matches played; "warm" means 3 or more.
- If `r < 0.4` and both new and warm candidates exist: pair a random new candidate with the warm candidate nearest in Elo.
- Else, if `r < 0.8` and there are at least 2 warm candidates: among warm pairs with |ΔElo| ≤ 200 (stop after 20 pairs), sample with weight `exp(-Δ/200) * max(1 - sim, 0.05)`.
- Otherwise: pick a random pair from the top half by Elo, with at least 2 candidates.

`select_mode` returns `"debate"` when `min(matches_played) < 2` or |ΔElo| < 50, else `"pairwise"`.

- [ ] **Step 1: Write the failing tests:**
  - `test_update_matches_reference_values`: 1200 vs 1200, a wins, min_played 0 gives (1216.0, 1184.0); min_played 6 gives (1208.0, 1192.0).
  - `test_elo_replay_is_idempotent`: appending the same match twice leaves the Elo and the match count unchanged.
  - `test_invalid_match_no_change`
  - `test_select_pair_seeded_is_deterministic`: the same seed gives the same pair.
  - `test_select_pair_prefers_distant_ideas`: over 200 seeded draws among warm candidates, the low-similarity pair is chosen more often than the high-similarity pair.
  - `test_select_mode_rules`
  - `test_reality_calibration_spearman`: perfectly monotone gives 1.0, and n < 3 gives `None`.
  - `test_eligible_excludes_executed_rejected_invalid`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement both modules.
- [ ] **Step 4:** Run them. Expected: PASS. Commit: `feat(ideation): candidate pool, elo replay, pair selection`.

---

### Task 5: Role prompts, renderers and parsers

**Files:**
- Create: `src/prompts/ideation/*.md` (the nine files listed in File Structure), `src/ideation/roles.py`
- Test: `tests/ideation/test_roles.py`

**Interfaces:**
- Consumes: `normalize_idea_bundle` (`src/orchestrator/idea_bundle.py`), `render_pack`, `pack_ids`, `PoolState`, `reality_calibration`
- Produces:
  - `RoleName = Literal["generate","reflect","judge_pairwise","judge_debate","evolve_combine","evolve_simplify","evolve_out_of_box","evolve_feasibility","metareview"]`
  - `@dataclass(frozen=True) class RoleContext: task_goal: str; task_rubrics: str; harness_rubric_requirements: str; recent_attempts: str; best_manifest: str; literature_pack: str; allowed_citations: frozenset[str]; feedback: str; reality: str`
  - `render_role(role: RoleName, ctx: RoleContext, **slots: str) -> str`, which fills `{{UPPER_SNAKE}}` placeholders. A leftover `{{…}}` raises `ValueError`.
  - `OUTPUT_FILE: Mapping[RoleName, str]`: `candidate.json` for generate and evolve_*, `review.json` for reflect, `verdict.json` for judge_*, and `feedback.json` for metareview.
  - `parse_candidate(raw: Any, *, cycle: int, strategy: str, allowed: frozenset[str]) -> Candidate`. It raises `RoleOutputError`. It requires `prediction` and `falsifier`, validates through `normalize_idea_bundle`, sets the id per Global Constraints, and drops citations outside `allowed`.
  - `parse_review(raw, *, candidate_id, allowed) -> Review`
  - `parse_verdict(raw) -> Literal["a","b"]`, which reads `{"winner": "a"|"b", "rationale": str}`.
  - `parse_feedback(raw) -> dict`, with keys `common_weaknesses`, `common_strengths`, `focus_areas` (lists of str) and `narrative` (str).
  - `class RoleOutputError(ValueError)`

**Prompt content requirements**, adapted from Co-Scientist with v3-specific additions:
- **generate:** literature-grounded, exactly one candidate. Output the idea-bundle schema from `src/prompts/idea.md` plus `rationale` and `citations` (pack ids only). Include `{{HARNESS_RUBRIC_REQUIREMENTS}}`, `{{RECENT_ATTEMPTS}}`, `{{FEEDBACK}}` and `{{REALITY}}`. Say outright that the candidate must be implementable through the task's documented candidate interface.
- **reflect:** the Co-Scientist review (novelty, correctness, testability) plus implementability against `{{TASK_GOAL}}`, and `already_tried` against `{{RECENT_ATTEMPTS}}`. Verdict enum as in `Review`.
- **judge_pairwise and judge_debate:** the Co-Scientist ranking prompts. The judging criteria are the task rubrics and preference for a measurable `combined_score` gain. Include `{{REALITY}}`, so the judge sees how well past Elo predicted outcomes.
- **evolve_*:** the four Co-Scientist strategies. Output the same candidate schema, with `parent_ids` set.
- **metareview:** the Co-Scientist system-feedback prompt, with an extra `{{OUTCOMES}}` slot for executed results.

- [ ] **Step 1: Write the failing tests:**
  - `test_render_every_role_has_no_leftover_placeholders`, parametrized over `RoleName`.
  - `test_parse_candidate_requires_preregistration`
  - `test_parse_candidate_drops_unknown_citations`
  - `test_parse_candidate_id_is_deterministic`
  - `test_parse_review_rejects_unknown_verdict`
  - `test_parse_verdict_accepts_only_a_or_b`
  - `test_prompts_carry_attribution_header`: every file under `src/prompts/ideation/` that is adapted from a Co-Scientist prompt starts with the attribution comment.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Write the prompts and `roles.py`.
- [ ] **Step 4:** Run them. Expected: PASS. Commit: `feat(ideation): role prompts adapted from co-scientist`.

---

### Task 6: Role runner script

**Files:**
- Create: `src/scripts/ideation_role.sh`, `src/ideation/role_runner.py`
- Test: `tests/ideation/test_role_runner.py`. Use the fake-headless pattern from `tests/investigation/test_campaign_ideas.py::test_shell_renders_feedback_and_shared_cap_without_evaluating_data`, via `script_tests._write_freeform_verification_agent`.

**Interfaces:**
- Consumes: `src/scripts/lib/container_agent.sh` (`run_agent_prompt`, `write_agent_prompt_file`, `record_headless_cost_log`, `validate_agent_reasoning_effort`)
- Produces:
  - The script's CLI: `bash src/scripts/ideation_role.sh --agent A [--agent-model M] [--reasoning-effort E] --output-dir D --prompt-file P --expected-output NAME.json [--containerize --container-image … --container-network … --container-auth-root … --container-env-file …]`. It exits 0 only when `D/NAME.json` exists and is a JSON object.
  - `@dataclass(frozen=True) class RoleAgent: agent: str; model: str | None; reasoning_effort: str | None`
  - `run_role(role: RoleName, prompt: str, *, out_dir: Path, agent: RoleAgent, timeout_seconds: int, container: ContainerOptions | None, runner: Callable[[list[str], int], int] | None = None) -> dict | None`. It writes `out_dir/prompt.md`, runs the script and returns the parsed JSON. It returns `None` on nonzero exit, timeout or invalid JSON, and never raises for agent failures.
  - `ContainerOptions` is a frozen dataclass mirroring the container kwargs of `run_idea_generation`.

The script's workspace is `--output-dir` only. It receives no task directory and no results directory, because the prompt carries all context. Mirror `idea.sh`'s agent launch block (around lines 880–1080: prompt file, `run_agent_prompt`, cost log, interrupt trap). Do not copy its task-staging code.

- [ ] **Step 1: Write the failing tests:**
  - `test_role_script_returns_json`: a fake agent writes `verdict.json`, and `run_role` returns the dict.
  - `test_role_script_missing_output_returns_none`
  - `test_role_script_invalid_json_returns_none`
  - `test_invalid_judge_output_no_elo_change`: `run_role` returns `None`, so a `Match(winner=None)` is recorded and the Elo is unchanged. This exercises `PoolStore`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement the script and `role_runner.py`.
- [ ] **Step 4:** Run them, then `bash -n src/scripts/ideation_role.sh`. Expected: PASS. Commit: `feat(ideation): headless role runner`.

---

### Task 7: The tournament round and the seam

**Files:**
- Create: `src/ideation/round.py`
- Test: `tests/ideation/test_round.py`, with a fake `run_role` that returns scripted JSON per role.

**Interfaces:**
- Consumes: everything from Tasks 1–6, plus `summarize_recent_attempts` (`src/investigation/harness_feedback.py`) and `IdeaGenerationResult`.
- Produces:
  - `@dataclass(frozen=True) class RoundSettings`: `per_round: int = 3`, `matches_per_round: int = 6`, `evolve_min_mature: int = 4`, `evolve_mature_matches: int = 2`, `evolve_top_k: int = 4`, `evolve_strategies: tuple[str, ...] = ("combine","simplify","out_of_box")`, `metareview_every: int = 3`, `pack_k_generate: int = 8`, `pack_k_reflect: int = 6`, `dedup_threshold: float = 0.85`, `max_pool: int = 40`, `seed: str = ""`, `role_timeout_seconds: int = 1800`.
  - `run_tournament_round(*, cycle: int, store: PoolStore, corpus: Sequence[LiteratureRecord], ctx_base: RoleContext, settings: RoundSettings, generator: RoleAgent, judge: RoleAgent, run_role_fn, work_dir: Path, history_titles: Mapping[str, str], log_event_fn) -> Candidate | None`
  - `make_tournament_idea_generation(cfg: InvestigationConfig, *, corpus_dir: Path | None, run_role_fn=run_role, log_event_fn) -> Callable[..., IdeaGenerationResult]`. The returned callable accepts the exact keyword arguments of `run_idea_generation` and ignores the ones it doesn't use. It builds `RoundSettings` from `cfg.ideation`, with `seed=results_dir.name`, and the generator `RoleAgent` from the per-cycle `agent` and `agent_model` kwargs.
  - `record_outcome(results_dir: Path, *, idea_id: str, cycle: int, attempt_id: str, combined_score: float | None, baseline_score: float | None, attempt_pass: bool, falsifier_outcome: str) -> None`. It is a no-op when `idea_id` is not in the pool.

**Round order.** All role outputs go under `work_dir/rounds/<cycle:04d>/<step>/`, and a step whose output already exists is not re-run. This gives resume.
1. Generate up to `per_round` new candidates, regardless of pool size. Each candidate is parsed, then deduped against the pool and `history_titles`; a duplicate is marked `rejected` with reason `duplicate:<id>`.
2. Reflect on each new candidate. `pass` and `neutral` become `eligible`; other verdicts become `rejected`.
3. Run `matches_per_round` matches among eligible candidates. Seed the RNG with `f"{settings.seed}:{cycle}:{match_index}"`.
4. Evolve if the number of eligible candidates with at least `evolve_mature_matches` matches is ≥ `evolve_min_mature`. Run each strategy once on the top `evolve_top_k`; the children are reflected now and compete from the next round.
5. Run meta-review if `cycle % metareview_every == 0`.
6. Retire eligible candidates beyond `max_pool`, lowest Elo first.
7. Return the top eligible candidate, or `None`.

**The seam callable.**
- If `validation_feedback` is non-empty, mark the previously selected candidate of this cycle `invalid` and select the next eligible one without running a new round.
- Otherwise run the round and write `bundle` to `output_dir/idea_bundle.json` and `target_rubrics.json`, both with `idea_id=candidate.id`. Also write `output_dir/wrapper_output.log` with a one-paragraph round summary. Return exit code 0.
- If there is no candidate, return exit code 1 with output `ideation_no_eligible_candidate`.

- [ ] **Step 1: Write the failing tests:**
  - `test_round_selects_top_elo_and_writes_bundle`: the files written pass `load_idea_artifacts(..., require_preregistration=True)`.
  - `test_round_resume_skips_completed_matches`: interrupt after 3 matches, rerun, and check the fake judge was called exactly `matches_per_round` times in total.
  - `test_round_generates_when_pool_exhausted`
  - `test_round_with_no_eligible_candidate_fails_cleanly`
  - `test_validation_feedback_picks_next_candidate`
  - `test_evolution_children_have_parents_and_wait_a_round`
  - `test_record_outcome_marks_executed_and_feeds_reality`: after 3 outcomes, `ctx.reality` in the next round's prompts contains `spearman`.
  - `test_judge_not_independent_logged_once`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement `round.py`.
- [ ] **Step 4:** Run them. Expected: PASS. Commit: `feat(ideation): tournament round and idea-generation seam`.

---

### Task 8: Configuration, wiring and the outcome hook

**Files:**
- Modify: `src/investigation/config.py` (add `IdeationConfig`, `IdeationLiteratureConfig`, `IdeationJudgeConfig`, and the field `ideation` on `InvestigationConfig`)
- Modify: `src/investigation/config_loader.py`, `configs/config.yaml` (an `ideation:` block with the defaults below)
- Modify: `src/investigation/parallel_helpers.py` (forward `ideation.*` to workers, and set `ideation.literature.corpus_dir=<parent corpus dir>`)
- Create: `src/ideation/backends.py`
- Modify: `src/investigation/main_entry.py`. Before `campaign_loop.run_cycles_and_finalize`, call `backend = get_backend(cfg.ideation.backend)`, then `backend.prepare(cfg, results_dir=…, log_event_fn=…)`. The coscientist backend builds or loads the corpus in `prepare`, and skips the build when `corpus_dir` is set. Then pass `run_idea_generation_fn=backend.idea_generation_fn(default=run_idea_generation_fn)` and `record_idea_outcome_fn=backend.record_outcome`.
- Modify: `src/investigation/campaign_loop.py`. Add the keyword parameter `record_idea_outcome_fn: Callable[..., None] = lambda **_: None`, and call it right after the ledger row is appended, with the ledger fields. Exceptions from the hook are logged as `ideation_outcome_failed` and swallowed.
- Regenerate: `tests/fixtures/pipeline_effective_config/expected.json` via `PIPELINE_EFFECTIVE_CONFIG_REGEN=1 ./.venv/bin/pytest -q tests/pipeline/test_pipeline_phases.py -k test_fixture_covers`
- Test: `tests/ideation/test_wiring.py`, `tests/investigation/test_campaign_ideas.py`

**Backend interface** (`src/ideation/backends.py`):
- `class IdeationBackend(Protocol)` has:
  - `name: str`
  - `prepare(self, cfg: InvestigationConfig, *, results_dir: Path, log_event_fn) -> None`
  - `idea_generation_fn(self, *, default: Callable[..., IdeaGenerationResult]) -> Callable[..., IdeaGenerationResult]`
  - `record_outcome(self, results_dir: Path, **ledger_fields) -> None`
- `SakanaScientistV3Backend`: `prepare` is a no-op, `idea_generation_fn` returns `default` unchanged, and `record_outcome` is a no-op.
- `CoScientistBackend`: `prepare` builds or loads the corpus and validates sources. `idea_generation_fn` returns `make_tournament_idea_generation(...)`, and `record_outcome` delegates to `round.record_outcome`.
- `BACKENDS: Mapping[str, Callable[[], IdeationBackend]] = {"sakana-scientist-v3": …, "coscientist": …}`
- `get_backend(name: str) -> IdeationBackend` raises `ValueError(f"unknown ideation.backend {name!r}; choose one of: coscientist, sakana-scientist-v3")`.

**Config defaults** (`configs/config.yaml`):

```yaml
ideation:
  backend: sakana-scientist-v3   # sakana-scientist-v3 | coscientist
  literature:
    sources: []             # e.g. ["folder:/path", "arxiv:video diffusion drift|max=50", "iclr:2025,2026:diffusion polic"]
    corpus_dir: null        # prebuilt corpus; skips building
    require_nonempty: true
  judge:
    agent: null             # null = same as agent.name
    model: null
    reasoning_effort: null
  per_round: 3
  matches_per_round: 6
  evolve_min_mature: 4
  evolve_top_k: 4
  metareview_every: 3
  dedup_threshold: 0.85
  max_pool: 40
  role_timeout_seconds: 1800
```

Validation in `config_loader`:
- An unknown `backend` raises the `get_backend` error at config load time.
- `backend=coscientist` with empty `sources` and no `corpus_dir` raises an error naming both keys.
- `backend=coscientist` forces `execution.require_preregistration=true` and logs `ideation_forced_preregistration`.
- Every entry of `sources` must pass `parse_source_spec`, and the error names the failing entry.

- [ ] **Step 1: Write the failing tests:**
  - `test_default_backend_keeps_runner`: with the default config, `main_entry` passes the original `run_idea_generation` through and the outcome hook is a no-op.
  - `test_unknown_backend_lists_choices`
  - `test_coscientist_backend_requires_sources`
  - `test_coscientist_backend_forces_preregistration`
  - `test_bad_source_spec_is_reported`
  - `test_campaign_calls_outcome_hook_with_ledger_fields`: in `test_campaign_ideas.py`, using the `_run_campaign` helper with `_registered_idea_generation`.
  - `test_outcome_hook_failure_does_not_stop_campaign`
  - `test_parallel_workers_receive_corpus_dir`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement the changes and regenerate the golden fixture. The fixture diff must contain only `ideation.*` keys; check with `git diff tests/fixtures | grep '^[+-] ' | grep -v ideation`, which should print nothing.
- [ ] **Step 4:** Run `env -u HEADLESS_BILLING timeout 580 python -m pytest tests -q -n 6`. Expected: all pass.
- [ ] **Step 5:** Commit: `feat(investigation): add ideation.backend with sakana-scientist-v3 and coscientist`.

---

### Task 9: Docs and a real smoke run

**Files:**
- Create: `docs/ideation.md`. Cover the source grammar with your example folder, the config table, the outputs layout, the cost model (about `per_round*2 + matches_per_round + evolve` role calls per cycle; with defaults that is roughly 12–16 agent calls), and the judge-independence advice.
- Modify: `README.md` (one line under "More examples" linking to `docs/ideation.md`)

- [ ] **Step 1: Build a real corpus:**
  `agsci literature build --source "folder:/home/s224075134/scratch/agentic/scientist-agent/sputnik/outputs/literature/conference_collections/20261123-075113-diffusion-policy-2025-2026" --source "arxiv:diffusion policy|max=20" --out /tmp/claude-344463/-weka-s224075134-agentic-scientist-agent-AI-Scientist-v3/5d4a2b60-5ff2-4cb2-9b4a-9bfa6d89ca1a/scratchpad/lit`
  Expected: the folder count is 24, the arXiv count is ≤ 20, and `errors` is empty or lists arXiv only.
- [ ] **Step 2: Smoke run on circpack** (CPU, cheapest):
  `agsci run examples/circpack execution.phases=investigation investigation.execution.max_idea_cycles=2 investigation.ideation.backend=coscientist investigation.ideation.per_round=2 investigation.ideation.matches_per_round=2 'investigation.ideation.literature.sources=["arxiv:circle packing|max=10"]'`
  Expected: `results/circpack/<agent>/<run>/investigation/ideation/` has `candidates.jsonl`, `matches.jsonl` and `outcomes.jsonl` with 2 rows, and `prediction_ledger.jsonl` has 2 rows whose `idea_id` starts with `cand_`.
- [ ] **Step 3:** Write the observed counts and wall time into `docs/ideation.md` under "Measured cost". Commit: `docs(ideation): usage, cost and smoke-run results`.


## Implementation record (2026-11-24)

Tasks 0–8 implemented in order and committed separately. Task 9 documentation is
written; both live commands and measured-cost claims are intentionally skipped at
the user's request. Offline source fixtures are constructed in the test modules
rather than separate fixture files. The existing virtualenv is prepended to PATH
because system `python` has no pytest. Golden regeneration changes only `ideation.*`.

Review fixes add ledger-to-pool reconciliation, derive executed status from durable
outcomes, recheck duplicates after interrupted ingest, and disable shared container
sessions for individual roles. Evolution reflections are included in cost estimates.

Ruling: literature access is prohibited in role prompts, matching the existing
idea runner's policy, rather than claiming a hard network sandbox. The shared
headless launcher grants broad tools and needs provider API connectivity; forcing
Docker `network=none` would break ordinary hosted agents. Strict provider-only egress
needs runtime infrastructure beyond this plan. Cost if wrong: a noncompliant role
could use external literature. This limitation is documented explicitly.

Ruling: old ledger rows lacking `attempt_pass` recover it from attempt history,
or conservatively use false if the crash occurred before history append. New ledger
rows persist the pass flag before the pool hook. Cost if wrong: a legacy recovered
row can underreport pass status; score and calibration data remain intact.

Verification: new task tests passed. Four pre-existing campaign tests fail because
this host's `/tmp` is owned by `nobody`, rejected by the existing storage validation.
The full-suite result is reported separately after its single final run.
