# Research discovery implementation plan

**Spec:** `docs/superpowers/specs/2026-11-25-research-discovery.md` (read in full).
**Execution:** five phases, TDD, one commit each on `feat/proposal-mode`; no push, live roles or network required. Work in place as explicitly requested. Protect the operator's existing dirty files byte-for-byte.

## Design and interfaces

Use a standalone `src/research_discovery/` package. Source records reuse `LiteratureRecord`; immutable passages and checked quote spans form the claim graph. The local role adapter invokes the existing runner's `generate` transport with discovery-specific prompts and independent output validators; it does not mutate the shared role registry or shell whitelist. Every role and retrieval step has an input-keyed atomic checkpoint. Model failures and malformed outputs never become completed steps.

Published design ideas are implemented as described in the operator spec (recurring limitations, cross-method comparison, claim-to-gap provenance, value axes). No reference repository is present locally; no third-party code or prompts will be copied or license compatibility asserted.

### Phase 1 — retrieval and extraction

Create `models.py`, `config.py`, `artifacts.py`, `providers.py`, `ingest.py`, `extraction.py`, module-local prompts and `tests/research_discovery/test_extraction.py` / `test_retrieval.py`.
- Load folder PDF/Markdown/metadata, BibTeX, arXiv/DOI lists, and Markdown/LaTeX/PDF review manuscripts. Resolve manuscript citation aliases and retain section/paragraph provenance.
- Search scholarly sources and expand citations through injectable, bounded providers; retrieve local/available OA full text. Cache records and documents with search errors visible.
- `extract_claims(raw, documents)` validates strict claim schemas and exact source/passages/character spans, drops unsupported claims, and preserves uncertainty and manuscript origin.
- RED→GREEN tests: input types, manuscript citation mapping, valid quotes, invented quotes/source IDs dropped, offline scholarly search/citation expansion/full-text fixtures, failed-output retry/resume.

### Phase 2 — gap discovery

Create `discovery.py`, discovery prompt and tests. `discover_gaps(raw, claims, documents)` constructs stable `ResearchGap` objects from recurring limitations, method comparisons and contradictions; requires multiple paper sources, checks claim edges, deduplicates equivalent gaps, and labels explanations as hypotheses. Tests cover grounded/distinct candidates, fabricated links, duplicate candidates and unsupported causal assertions.

### Phase 3 — adversarial verification

Create `verification.py`, verifier prompt and tests. Search separately for solutions/counterevidence per gap, retain new corpus records/passages, and validate `open|partially_addressed|already_resolved|uncertain`. Resolution requires a resolving source quote; no search hits without positive evidence yields uncertain; failures remain visible. Test existing solutions, contradictory results, empty/failed search, unsupported verdicts and resume.

### Phase 4 — assessment and ranking

Create `assessment.py`, assessment prompt and tests. Validate importance/difficulty/impact/feasibility scores and evidence-backed rationales. Rank supported open/partial gaps deterministically with a documented weighted score; retain rejected/uncertain gaps unranked. Tests exercise all axes, invalid/NaN/bool scores, fabricated evidence, difficulty direction, ties and excluded statuses.

### Phase 5 — orchestration and reporting

Create `driver.py`, `report.py`, `cli.py`, docs and end-to-end tests; add only `discover` dispatch in `src/agsci/main.py`.
- Assemble resumable retrieve→extract→discover→verify→assess→report workflow, with changed-input rejection and incremental verification corpus snapshots.
- Write structured research gaps, readable evidence/source report, corpus and passages, status and compatible `gaps.json` handoff. Only open/partial gaps and actual corpus L IDs enter the handoff; no proposal wiring.
- Test kill/resume without repeated completed roles, resolved/uncertain exclusions, handoff citation validity, CLI dispatch and backward compatibility.

## Verification and review focus

Run `env -u HEADLESS_BILLING .venv/bin/python -m pytest -q tests/research_discovery tests/ideation` and Ruff after each phase. Check protected-file hashes before each commit; explicitly stage only owned files. Final review covers source/citation spoofing, review claims mistaken for independently verified findings, failed/empty counter-search, crash checkpoints and ranking unsupported gaps. No live integration validation; provider/model availability and semantic quote entailment remain explicit limitations rather than claims of proof.

## Execution record

- Phase 1: `89d46b0` — retrieval/ingestion/extraction; 113 combined tests passed.
- Phase 2: `7865c3a` — distinct source-linked gap hypotheses; 117 combined tests passed.
- Phase 3: `00b9342` — independent verification and counterevidence; 121 combined tests passed.
- Phase 4: `3971c69` — justified value axes and deterministic ranking; 130 combined tests passed.
- Phase 5: end-to-end driver, reporting, CLI and boundary/resume regression coverage. Final combined suite: 151 passing tests; Ruff clean; Mypy clean across 459 files with ignored local `tasks/` excluded.
- Final integration corrections cover duplicate scientific sources, PDF section provenance, LaTeX citation notes, manuscript-only identifier seeds, partial JSON recovery, frozen verification contexts, offline/zero-budget searches, bounded role contexts, and unknown inline handoff citations.
- Interface ruling: the protected `parse_gaps` requires 3–8 supported gaps. Export at most eight; export fewer honestly with `proposal_handoff_ready=false` rather than invent evidence.
- Packaging ruling: the spec's external-edit whitelist excludes `pyproject.toml`. Source/editable usage is supported; adding the new package to the repository's explicit wheel list remains a follow-up packaging change.
- No real agents or network calls were run. No external reference code was copied. All 16 pre-existing protected files passed byte-for-byte hash checks. No push.
