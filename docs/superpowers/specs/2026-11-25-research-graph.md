# Research discovery v2: read relevant papers selectively, then find gaps in a research graph

This changes `src/research_discovery/`, which was built from `2026-11-25-research-discovery.md`. Retrieval, quote/provenance checks, verification, assessment, report and the `gaps.json` handoff all stay. The task replaces per-paper extraction and flat claim-list discovery with the design below. The approach follows SGHA (https://github.com/SarveshVGharat/structural-gap-hypothesis-agent). Check its license before copying any code, and record provenance in file headers.

## Why

The live run on the diffusion-policy corpus (200 papers, 155 full text) would have made 840 sequential Codex extraction calls. It extracted chunks of 8 passages from every section of every paper. The operator wants:
- only the relevant papers read;
- selective reading;
- gaps that come from cross-paper structure (methods, assumptions, failure conditions), not from what one paper says about itself.

## Pipeline

1. **Retrieval.** Unchanged. It must reuse an existing `retrieval/completed.json`: a new setting or flag must not invalidate the cached retrieval of an existing run dir. Add a test for that.

2. **Relevance ranking.** Pure code, no LLM. Score each paper by:
   - BM25/TF-IDF similarity of the topic to its title and abstract;
   - how often the operator manuscript cites it, and in how many sections;
   - in-corpus citation links (cited by, or citing, high-scoring papers).

   Choose tiers from the scores:
   - `deep`: top `--deep-papers` (default 40), with about 10% of those slots reserved for lower-ranked papers linked to the top ones (exploration);
   - `abstract`: all remaining papers.

   Write `relevance.json` with each paper's scores and tier.

3. **Selective extraction into graph fragments.**
   - **`deep` papers:** only the abstract, introduction, limitations/discussion, conclusion and future-work passages (match on section names, up to 8 passages per paper), with 2–3 papers per call.
   - **`abstract` papers:** 10 abstracts per call.
   - **The manuscript:** extracted in full, in batches. Its citations resolve to paper ids as they do now.
   - **Concurrency:** run up to `--parallel` (default 4) role calls at once. Each step is still cached individually and resumable.

   Each call returns typed nodes and edges:
   - node types: `problem`, `method`, `assumption`, `failure_condition`, `setting`, `finding`;
   - edge types: `addresses` (method→problem), `assumes` (method→assumption), `fails_under` (method→failure_condition), `evaluated_on` (method→setting), `extends` (method→method), `reports` (paper→finding), `contradicts` (finding→finding), `handles` (method→failure_condition or assumption it removes).

   Every edge carries evidence spans that the existing quote check validates. Drop an edge if its evidence fails the check, and log it. Papers are graph nodes, and each edge records its source paper.

4. **Canonicalize.** TF-IDF/character similarity clusters near-duplicate node names of the same type. Then one LLM role confirms or splits each cluster, using strict output validation. Write `graph.json`: nodes with their aliases, edges with their provenance, and paper nodes with their tier.

5. **Structural gap detectors.** Pure code; each returns candidates with the node and edge ids that support them.
   - **`shared_assumption`:** an assumption held by at least k methods addressing a problem, with no `handles` edge removing it.
   - **`recurring_failure`:** a failure condition linked by `fails_under` to methods from at least 2 unrelated method clusters, with no `handles` edge.
   - **`uncovered_combination`:** a problem × setting pair where both are well populated but no method addresses the problem in that setting.
   - **`contradiction`:** findings joined by a `contradicts` edge, or opposite results reported for the same method and setting.
   - **`missing_bridge`:** two method clusters addressing the same problem with no `extends` or comparison edge between them.
   - **`evaluation_gap`:** methods for a topic problem never `evaluated_on` the settings the topic names.

6. **Screen.** One LLM role ranks the structural candidates and rejects trivial ones, giving a reason for each rejection. The top `--max-gaps` (default 10) become ResearchGap objects, each with a `pattern` field and its graph evidence.

7. **Targeted deep reading.** For each surviving gap, read the methods and results passages of its supporting `abstract`-tier papers (one call per gap, capped). Add the edges this produces to the graph. Re-check the gap's pattern against the updated graph, and drop the gap if the pattern no longer holds.

8. **Verify and assess.** Run the existing verification (counter-searches, verdict labels) and the existing assessment and ranking.

9. **Problem families.** Group related gaps (for example by shared nodes) into research-problem families, in one LLM role or in code.

10. **Report.** Extend the existing report and write `gaps.json` as before, now with `pattern` and family included. Add `roadmap.md`: per topic problem, the methods addressing it, the assumptions they share, their failure conditions and the uncovered cells, plus a Mermaid graph of the top problems.

## Constraints

- Keep the boundaries of the first spec. Do not edit `src/ideation/` or `src/prompts/ideation|proposal/`. Prompts live in `src/research_discovery/prompts/`. Validate every LLM output strictly.
- Keep the operator's uncommitted proposal-mode gap files untouched and uncommitted: `src/ideation/proposal/*`, `src/prompts/proposal/*`, `src/ideation/roles.py`, `src/scripts/ideation_role.sh`, `tests/ideation/test_proposal_*`, `src/prompts/proposal/gap_analysis.md`.
- **First commit.** Commit the already-made OpenAlex key change (`src/research_discovery/providers.py` and its test in `tests/research_discovery/test_retrieval.py`) as its own commit. Never commit `.env`.
- **Rate limits.** Handle provider rate limits:
  - honour `Retry-After`, and stop calling a provider once it reports more than 60 s, recording that in the audit;
  - write a warning to the log for every failed search;
  - warn when `--max-papers` is not above the seed count, since in that case no online papers can be added.
- **Tests.** Write tests with fake roles and no network. They must cover:
  - relevance tiers and exploration slots;
  - section selection;
  - edges dropped when their quote is bad;
  - canonicalization merges;
  - each detector positive and negative;
  - targeted reading that removes a gap;
  - resume after a kill;
  - a cached retrieval reused under new settings.

  Run `env -u HEADLESS_BILLING .venv/bin/python -m pytest -q tests/research_discovery tests/ideation`, and keep ruff and mypy clean.
- **Calls.** Log the estimated number of LLM calls before extraction starts.
- **No live runs.** Do not run real agents or live searches; the supervising session will run the real run.
- Commit per phase on `feat/proposal-mode` and do not push.
- **Report.** When done, report files, tests, the estimated call count for a 200-paper corpus with `--deep-papers 40`, and anything not implemented.
