# Proposal Mode Implementation Plan

> **For agentic workers:** implement task by task, TDD, one commit per task. Run tests with `env -u HEADLESS_BILLING .venv/bin/python -m pytest -q tests/ideation`. Tokens are scarce: read only the files a task names.

**Goal:** `agsci propose` turns a free-text research goal (plus an optional codebase) into tournament-ranked research proposals and a final overview, using literature and debate only, with no experiments. A human then picks which proposals deserve a real experiment, and `agsci run` executes only those.

**Why:** some experiments (video diffusion, large diffusion policies) are too heavy to run one per idea. Ranking on paper first, as Google's Co-Scientist does, decides where GPU time goes.

**Architecture:** reuse the existing tournament (`src/ideation/round.py`, pool, Elo, BM25 literature packs, role runner). Parametrize the round with a `Profile` that owns the prompt directory, the candidate parser and the candidate text. The existing behaviour becomes the `execution` profile; the new `proposal` profile has its own prompts and schema. A small driver loops rounds until a round cap or Elo stability, then writes `proposals.jsonl` and `overview.md`.

**Reference:** Co-Scientist reimplementation at `/home/s224075134/scratch/agentic/scientist-agent/Co-Scientist` (`config/prompts/parse_goal.md`, `generation_debate.md`, `reflection_verification.md`, `metareview_final.md`). Adapt, keep the Apache-2.0 attribution line at the top of each adapted prompt, as `src/prompts/ideation/generate.md` already does.

## Global Constraints

- No network for role agents. Literature comes only from the frozen corpus (`src/ideation/literature`). Cite only supplied `L`-prefixed pack ids; unknown ids are dropped and counted, as `_citations` does today.
- Every role step stays resumable through `completed.json` / `decision.json`, exactly like `run_tournament_round`. Rerunning `agsci propose run --out <same dir>` must resume without re-calling finished roles.
- The `execution` profile must keep current behaviour byte-for-byte: existing `tests/ideation` and `tests/investigation` pass unchanged.
- Role agents run natively in proposal mode (no container). The codebase is read-only for them; say so in the prompt.
- New code stays inside `src/ideation/` (plus `src/agsci/main.py` dispatch, prompts, tests, docs).

## Data shapes

`plan.json` (output of the `parse_goal` role, validated by `parse_plan`):
`{objective: str, preferences: [str], constraints: [str], idea_attributes: [str], focus_axes: [str] (3-6 orthogonal directions), domain_hint: str|null, compute_note: str}`

`codebase.json` (output of `codebase_scan`, validated by `parse_codebase`):
`{summary: str, entrypoints: [{path, purpose}], components: [{path, role}], extension_points: [{path, description}]}`. Paths are relative to the codebase root; entries whose path does not exist are dropped.

Proposal candidate bundle (validated by `normalize_proposal`; id stays `cand_` + sha1(title+statement)[:10]):
`{idea_id, title, statement (one sentence), mechanism, anticipated_outcomes, novelty_argument, falsifier: {description, measurement}, first_experiment: {description, scale, compute_estimate_gpu_hours: number>=0, success_criterion}, codebase_touchpoints: [path], risks: [str], prediction: {confidence in [0,1]}}` plus `rationale`, `citations`, `parent_ids`. When a codebase is given, `codebase_touchpoints` must contain at least one existing path; nonexistent paths are dropped and counted.

Verification review (new `Review` field `assumptions`, default empty tuple so old rows still load):
`assumptions: [{claim, status: plausible|uncertain|implausible, rationale}]`, `weakest: str`, `kill_experiment: str`. Stored as a second review row with `kind='verification'` (add `kind: str = 'full'` to `Review`; `PoolState.reviews` keeps the `full` review per candidate, `PoolState.verifications` the verification one).

## Review Focus

1. Two generator calls in one round must not receive identical prompts (the duplicate bug seen in run `20261124_055537`): each call gets the round's and pool's existing titles plus a distinct focus axis.
2. Resume after a kill mid-round re-runs no finished role and appends no duplicate pool rows.
3. A proposal citing a path outside the codebase root (`../`, absolute) is dropped, never read.
4. Elo-stability stop: top-k ids unchanged for `stable_rounds` consecutive rounds stops early; fewer than 2 eligible candidates stops with a clear status, not a crash.
5. `overview.md` cites only candidate ids in the pool and `L` ids in the corpus; unknown ones are stripped by `render_overview`, not trusted from the model.

---

### Task 1: Profile seam in the round (refactor, no behaviour change)

**Files:** modify `src/ideation/round.py`, `src/ideation/roles.py`; create `src/ideation/profiles.py`; test `tests/ideation/test_profiles.py`.

**Interfaces (produces):**
```python
@dataclass(frozen=True)
class Profile:
    name: str                      # 'execution' | 'proposal'
    prompt_dir: Path               # src/prompts/ideation | src/prompts/proposal
    parse_candidate: Callable[..., Candidate]   # (raw, *, cycle, strategy, allowed) -> Candidate
    candidate_text: Callable[[Candidate], str]
    generate_strategies: tuple[str, ...]        # execution: ('literature',); proposal: ('literature','debate')
PROFILES: Mapping[str, Profile]
```
`render_role(role, ctx, *, prompt_dir=DEFAULT, **slots)`. `run_tournament_round(..., profile: Profile = PROFILES['execution'])`.

- [ ] Test: `run_tournament_round` with a fake `run_role_fn` produces the same pool rows with and without passing `profile=PROFILES['execution']`.
- [ ] Implement; existing tests unchanged and green. Commit `refactor(ideation): add tournament profile seam`.

### Task 2: Diversify generator calls (fixes Review Focus 1, both profiles)

**Files:** `src/ideation/round.py`, `src/prompts/ideation/generate.md`, test `tests/ideation/test_round.py`.

- Add slots `{{EXISTING_CANDIDATES}}` (titles of all pool candidates plus earlier candidates of this round, one per line, or `(none)`) and `{{FOCUS}}` to the generate prompt, with the instruction "Propose something materially different from every existing candidate; work along this focus."
- Focus for call `i`: `focus_axes[i % len]` when the context has axes (proposal), else the round's strategy name (execution). Strategy for call `i`: `profile.generate_strategies[i % len]`; strategy `debate` renders role `generate_debate` instead of `generate`.
- [ ] Test `test_generator_prompts_differ_within_round`: with per_round=3 and a recording fake runner, the three `generate_*/prompt.md` texts are pairwise different and the third contains the first two titles.
- [ ] Commit `fix(ideation): give each generator call the round's existing candidates and its own focus`.

### Task 3: Proposal schema, roles and prompts

**Files:** create `src/ideation/proposal/__init__.py`, `src/ideation/proposal/schema.py`, `src/prompts/proposal/{parse_goal,codebase_scan,generate,generate_debate,reflect,verify,judge_pairwise,judge_debate,evolve_combine,evolve_simplify,evolve_out_of_box,evolve_feasibility,metareview,overview}.md`; modify `src/ideation/pool.py` (Review.kind, assumptions, weakest, kill_experiment; PoolState.verifications), `src/ideation/roles.py` (RoleName gains `generate_debate`, `verify`, `parse_goal`, `codebase_scan`, `overview`; OUTPUT_FILE entries `plan.json`, `codebase.json`, `overview.json`); test `tests/ideation/test_proposal_schema.py`.

**Produces:** `parse_plan(raw) -> dict`, `parse_codebase(raw, root: Path) -> dict`, `normalize_proposal(raw, *, cycle, codebase_root: Path|None) -> dict`, `parse_proposal_candidate(raw, *, cycle, strategy, allowed, codebase_root=None) -> Candidate`, `parse_verification(raw, *, candidate_id, allowed) -> Review`, `PROFILES['proposal']`.

- Proposal prompts use slots `{{GOAL_PLAN}}`, `{{CODEBASE}}` (rendered `codebase.json` or `(no codebase)`), `{{LITERATURE_PACK}}`, `{{EXISTING_CANDIDATES}}`, `{{FOCUS}}`, `{{FEEDBACK}}`, plus the role-specific ones the execution prompts use (`CANDIDATE`, `CANDIDATE_A/B`, `PARENTS`, `OUTCOMES`). Judges rank on novelty, plausibility of the mechanism, strength of the falsifier, and cost of the first experiment relative to its information value. `generate_debate` asks for a simulated 3-5 turn expert discussion written to `transcript.md`, then the final candidate JSON. `verify` follows Co-Scientist `reflection_verification.md`.
- Reflect verdicts for proposals map onto the existing set: `pass`, `neutral`, `already_explained`, `disproved` (keep `Review.verdict` literal unchanged).
- [ ] Tests: valid proposal normalizes; missing falsifier/first_experiment raises `RoleOutputError`; `codebase_touchpoints` with `../x`, `/etc/passwd`, and a missing file are dropped and a candidate with none left fails when a codebase is given; verification with an unknown status raises; old `reviews.jsonl` rows without `kind` load as `full`.
- [ ] Commit `feat(ideation): add proposal schema, roles and prompts`.

### Task 4: Verification step in the proposal round

**Files:** `src/ideation/round.py`, test `tests/ideation/test_round.py`.

- In `reflect`, when `profile.name == 'proposal'` and the full review passed, run role `verify` (step dir `verify_<id>`) once; store via `add_review`. A verification whose weakest assumption is `implausible` marks the candidate `rejected` with reason `implausible_assumption`. Judges receive both reviews in `display(id)`.
- [ ] Test: fake runner returning an implausible weakest assumption rejects the candidate; a plausible one leaves it eligible; rerun does not call `verify` again.
- [ ] Commit `feat(ideation): verify proposal assumptions before the tournament`.

### Task 5: Proposal driver and outputs

**Files:** create `src/ideation/proposal/driver.py`, test `tests/ideation/test_proposal_driver.py`.

**Produces:**
```python
@dataclass(frozen=True)
class ProposalSettings:
    rounds: int = 4
    stable_rounds: int = 2
    top_k: int = 5
    round: RoundSettings = RoundSettings(metareview_every=1)
def run_proposal(*, goal: str, out_dir: Path, corpus: Sequence[LiteratureRecord],
                 codebase: Path | None, generator: RoleAgent, judge: RoleAgent,
                 settings: ProposalSettings, run_role_fn=run_role, log=print) -> Path
```
- Steps, each resumable: write `goal.md`; role `parse_goal` → `plan.json`; if codebase, role `codebase_scan` (prompt names the absolute codebase path, read-only) → `codebase.json`; then rounds `1..rounds` of `run_tournament_round(profile=PROFILES['proposal'])` with `RoleContext` built from the plan (`task_goal` = rendered plan) and `reality='(proposal mode: no experiments run)'`. After each round append `{round, top_k_ids, elo}` to `ideation/elo_snapshots.jsonl`; stop early when the last `stable_rounds` snapshots share the same top-k id set.
- Finish: write `proposals.jsonl` (eligible candidates by Elo: id, elo, matches_played, bundle, review, verification) and run role `overview` with the top-k → `overview.md`. `render_overview(raw, *, candidate_ids, citation_ids) -> str` strips unknown `[cand_*]` and `[L*]` references. Write `status.json` `{state: completed|stable|no_candidates, rounds_run, top_k}`.
- Overview structure (from Co-Scientist `metareview_final.md`): executive summary; directions each with claim, supporting candidate ids, why promising, open questions, falsifier, cheapest first experiment with compute estimate; convergence and divergence; caveats; sources (L ids only).
- [ ] Tests with a fake runner: full run writes plan, proposals, overview, status; stability stop after 2 identical snapshots; killed-and-rerun produces identical pool row counts; unknown ids stripped from the overview.
- [ ] Commit `feat(ideation): add proposal driver with Elo-stability stop and final overview`.

### Task 6: CLI and the human gate into execution

**Files:** create `src/ideation/proposal/cli.py`; modify `src/agsci/main.py` (command `propose`), `src/ideation/round.py` (`make_tournament_idea_generation` seeding), `src/investigation/config.py`, `src/investigation/config_loader.py`, `configs/config.yaml` (`ideation.seed_proposals: null`), golden fixture `tests/fixtures/pipeline_effective_config/expected.json` (regenerate with `PIPELINE_EFFECTIVE_CONFIG_REGEN=1`); docs `docs/ideation.md`; test `tests/ideation/test_proposal_cli.py`.

CLI:
```
agsci propose run (--goal TEXT | --goal-file F) [--codebase DIR] --source SPEC [--source SPEC...]
                  [--agent codex] [--model M] [--reasoning-effort E]
                  [--judge-agent A] [--judge-model M] [--rounds 4] [--per-round 3]
                  [--matches-per-round 6] [--out DIR]
agsci propose select RUN_DIR CAND_ID [CAND_ID...]   # writes RUN_DIR/selected.jsonl
```
Default `--out`: `results/proposals/<slug of objective words>/<YYYYMMDD_HHMMSS>/`. `--source` uses the existing source grammar and builds the corpus into `<out>/literature` (skip if it exists). Print the overview path and the top-k table at the end.

Execution hand-off: `investigation.ideation.seed_proposals=<RUN_DIR>/selected.jsonl` (coscientist backend only). On cycles with no pending selection, if seeded proposals remain unconverted, run one `concretize` role (execution prompt dir, new `src/prompts/ideation/concretize.md`) that turns the next selected proposal plus `task.md` into an executable idea bundle (implementation plan, rubrics, numeric falsifier), ingest it with strategy `proposal:<proposal id>`, reflect it, and select it ahead of Elo order. When all seeds are executed, fall back to normal rounds.
- [ ] Tests: argparse round-trip; `select` rejects unknown ids; seeded run converts and selects seeds in the given order before any generated candidate (fake runner).
- [ ] Commit `feat(agsci): add propose command and seed execution from selected proposals`.

### Task 7: Real check (manual, cheap)

`HOME=/weka/s224075134/.codex-weka-home .venv/bin/agsci propose run --goal "Reduce error accumulation in autoregressive video diffusion models" --source "arxiv:autoregressive video diffusion error accumulation|max=20" --agent codex --model gpt-6.2-sol --reasoning-effort medium --rounds 2 --per-round 3 --matches-per-round 3`. Pass: `overview.md` exists, candidates in round 1 are not duplicates, every citation id resolves. Do not run this without the operator's go-ahead.

## Interface review and execution record

- RoleContext gains defaulted goal_plan, codebase and focus_axes fields; proposal rounds bind codebase_root into the profile parser using dataclasses.replace / functools.partial.
- New RoleName members need execution-directory templates too because existing role rendering tests enumerate every role. Proposal-only roles default to their proposal templates; concretize uses execution templates.
- Task 6 adds concretize to RoleName and OUTPUT_FILE (candidate.json). The loader is src/investigation/config_loader.py.
- Proposal reality stays explicitly experiment-free instead of being replaced by execution calibration. Verification.weakest identifies an assumption claim; reject only if that claim has status implausible.
- Pre-flight: Tasks 1→3→4→5 share Profile/parser/context; Tasks 3→4→5 share Review.kind and verifications; Tasks 5→6 share proposals rows and selected.jsonl. Above corrections align these interfaces.
- User scope overrides skill review delegation and Task 7: no real agents, network, or push.
