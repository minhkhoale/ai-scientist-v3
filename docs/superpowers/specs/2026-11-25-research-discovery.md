# Research gap identification (`src/research_discovery/`)

## Operator request

Run research gap identification before ideas are generated. Inputs are:
- a list of existing papers, given as a folder of PDFs/markdown, a BibTeX file, or a list of arXiv ids/DOIs;
- a literature-review manuscript (markdown, LaTeX or PDF) written by the operator.

The output is a ranked list of research gaps that the proposal mode will later read.

## Task (operator's spec, verbatim)

Implement a new research_discovery module inside the existing AI Scientist-v3 repository.

Scope: Research gap identification only. Do not modify ideation, experiment execution, or paper writing.

Required workflow: Research topic → Literature retrieval → Scientific information extraction → Research gap generation → Adversarial gap verification → Importance/difficulty/impact/feasibility evaluation → Ranked research gap report.

Main requirements
- Inspect the current repository and reuse existing LLM, configuration, logging, and artifact utilities where appropriate.
- Retrieve actual scientific literature using scholarly search providers, with citation expansion and full-text retrieval where available.
- Extract structured problems, methods, assumptions, limitations, and findings with passage-level provenance.
- Identify gaps through recurring limitations, comparisons of existing methods, and unexplained contradictions.
- Analyze plausible reasons each gap remains unresolved without asserting unsupported causal explanations.
- Independently verify each gap by searching for existing solutions and counterevidence.
- Evaluate each gap's importance, difficulty, expected impact, and feasibility.
- Produce structured, ranked ResearchGap artifacts and a human-readable evidence-grounded report.
- Preserve uncertainty. Do not treat missing search results as proof that a gap is novel.
- Keep all existing AI Scientist-v3 workflows backward compatible.

Reference designs: SGHA for evidence-based gap discovery, IdeaScientist for methodological gap comparison, AutoIdea for claim-to-gap provenance, and Good Question for research-value evaluation. Reuse the ideas and permitted code where appropriate, subject to repository licenses.

Phases, each with focused tests that check extracted claims have valid supporting sources:
1. Literature retrieval and structured scientific extraction.
2. Candidate research gap discovery.
3. Adversarial gap verification.
4. Research value assessment and ranking.
5. End-to-end tests and research-gap reporting.

Acceptance: given a research topic and a literature corpus, the module produces distinct, source-grounded candidate gaps; detects already-resolved limitations; explicitly labels uncertain claims; and ranks supported gaps using justified importance, difficulty, impact, and feasibility assessments.

## Constraints from the supervising session

- **Inputs.** Seed the corpus from the operator's paper list and the manuscript. Parse the manuscript into its claims, and treat each claim with its citations as evidence carrying provenance (section and paragraph). Then expand through citations and search.
- **Reuse, read only.** Import from `src/ideation/literature/` (sources, corpus, retrieval), `src/ideation/role_runner.py` (agent roles through `src/scripts/ideation_role.sh`), and `src/ideation/literature/corpus._atomic_write`. Do not edit `src/ideation/`, `src/prompts/ideation/` or `src/prompts/proposal/`. Only additive changes are allowed outside the new module: a whitelist entry in `ideation_role.sh`, a CLI subcommand `agsci discover run`, config defaults, and docs.
- **Leave the existing work alone.** The working tree has uncommitted gap-analysis changes to proposal mode, in `src/ideation/proposal/*`, `src/prompts/proposal/*`, `roles.py`, `ideation_role.sh` and tests. Do not revert or edit them.
- **Provenance.** Every extracted claim has a quote span from a source record that the code checks; drop unsupported claims rather than keep them. Every LLM output is validated strictly at the boundary, the same way `src/ideation/proposal/schema.py` does it.
- **Verdict labels.** Verification gives each gap one of: `open`, `partially_addressed`, `already_resolved` (with the resolving source), or `uncertain`. "Search found nothing" must yield `uncertain` unless positive evidence supports `open`.
- **Resumable.** Each step caches its output (as with `completed.json` in the proposal driver), so a killed run resumes without repeating finished roles.
- **Handoff format.** Also write `gaps.json` as `{"gaps":[{id, statement, sub_problem, why_open, closing_evidence, closest_prior:[L ids], evidence:[{citation: L id, finding}]}]}`. Include only gaps that are `open` or `partially_addressed`, and only citations that exist in the corpus's L ids. This is the shape `parse_gaps` in `src/ideation/proposal/schema.py` accepts. Wiring it into `agsci propose run` is a later step; do not do it now.
- **Reference repos.** If the SGHA, IdeaScientist, AutoIdea and Good Question repositories are reachable, check their licenses before copying anything, and record provenance in file headers. Otherwise follow their published designs.
- **Tests.** Tests use fake role functions and network-free fixtures. Run them with `env -u HEADLESS_BILLING .venv/bin/python -m pytest -q tests/research_discovery tests/ideation`, and keep `ruff check` clean.
- **No real runs.** Do not run real agents or network searches beyond a single smoke call.
- **Commits.** Commit each phase separately on the current branch `feat/proposal-mode`. Do not commit the supervising session's uncommitted files. Do not push.
- **Report.** When done, list the files, the tests, and anything left unimplemented, with the reason.
