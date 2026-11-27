# Research gap identification

`agsci discover run` is an independent pre-ideation workflow. It retrieves
literature, extracts source-grounded claims, discovers candidate gaps, searches
for counterevidence, assesses research value, and writes ranked gap reports.
It does not change proposal generation, experiments, or manuscript writing.

```bash
agsci discover run \
  --topic 'Stable long-horizon diffusion policies' \
  --papers /path/to/papers \
  --papers /path/to/references.bib \
  --papers arxiv:2501.12345 \
  --papers doi:10.1234/example \
  --manuscript /path/to/review.md \
  --out results/discovery/example \
  --agent codex --judge-agent claude
```

Paper inputs are repeatable: folders containing `records.json`, Markdown,
BibTeX or PDFs; standalone BibTeX/PDF/Markdown files; identifier-list text files;
and individual arXiv identifiers or DOIs. `records.json` at the folder root takes
priority over recursively ingesting files and historical snapshots. Its local
`pdf_path` is used only when it resolves inside that folder. Manuscripts may be
Markdown, LaTeX or PDF. PDF text extraction requires `pdftotext`; there is no OCR.
DOI/arXiv identifiers appearing in the manuscript also seed scholarly lookups before citation expansion. The original files are read only. Put output outside every seed corpus directory.

## Retrieval and evidence

OpenAlex supplies topic searches, DOI resolution, backward references and forward
citations. arXiv identifiers use the arXiv metadata API. Optional `--source` values
reuse the existing literature grammar, for example `arxiv:diffusion policy|max=20`
or `iclr:2025,2026:diffusion polic|max=30`. Public PDF URLs and local PDFs supply
full text where available; inaccessible full text stays explicitly abstract-only.
Provider authentication, rate limits, incomplete metadata and text extraction
can limit coverage. The host retrieves literature; role agents are instructed
not to browse and use fresh native workspaces through the existing role runner.
Those agent restrictions are prompt policy, not an egress enforcement mechanism.

A manuscript paragraph retains section, paragraph number and declared citations.
BibTeX keys, `[@key]`, `\cite{key}` and numeric reference lists are resolved against
actual seed records. Unknown citations remain unresolved and force uncertainty.
A review claim is evidence of what the operator wrote, not independent confirmation
of a scientific finding. Duplicate titles are not counted as independent papers.

Every extracted claim has exact source/passage IDs, character offsets and a quote.
Code drops unsupported claims and rejects malformed model schemas. Gaps must link
claims from at least two paper sources; a manuscript does not count as a second
paper. Explanations of why a gap remains unresolved are always labeled hypotheses,
including when supporting observations exist. Quote identity does not prove that
the model's interpretation, comparison, or causal hypothesis is scientifically sound.

## Verification and value

Each gap receives separate solution and counterevidence searches and a fresh
verification role. The default verifier uses Claude independently of the Codex
generator; `--judge-agent`, `--judge-model`, `--model`, and `--reasoning-effort`
configure the agents. Same-agent/model verification is recorded as a limitation.

Verdicts are `open`, `partially_addressed`, `already_resolved`, or `uncertain`.
A resolution needs a resolving paper and its supporting quote. An empty search
without positive paper evidence yields `uncertain`, never a novelty claim.
Unavailable verification also yields uncertainty. `--offline` skips host retrieval
and counter-search; roles still run, but cannot establish an open gap independently.
Tests use injected fake roles/providers and never launch agents or access the network.

Importance, difficulty, impact, and feasibility are scored from 1 to 5, each with
source-backed justification and explicit uncertainty. Ranking uses:

```text
20 × (0.30 importance + 0.30 impact + 0.25 feasibility + 0.15 (6 − difficulty))
```

Partially addressed gaps receive a multiplier of 0.8. These are transparent product
heuristics, not calibrated probabilities or experimentally validated value measures.
Resolved and uncertain gaps are retained for inspection but excluded from ranking.

## Artifacts and resume

- `inputs.json`: input fingerprints, agents and settings; changed inputs require a new directory.
- `literature/`: initial immutable literature snapshot using the existing corpus format.
- `retrieval/`: cached lookups, topic searches, citation expansions, documents and retrieval errors.
- `steps/<step>/completed.json`: validated role outputs; successful roles are not called again.
- `claims.json`: checked scientific/manuscript claims and unsupported-claim drop reasons.
- `candidate_gaps.json`: candidate gaps and deduplication/drop reasons.
- `verification_searches/`: independent query results, retrieved passages and failed attempts.
- `documents.json`, `corpus/`: final source graph and immutable corpus including resolving/counterevidence sources.
- `research_gaps.json`: all processed candidates, evidence, verdicts and assessments.
- `ranked_gaps.json`, `report.md`: supported rankings and human-readable source/quote provenance.
- `gaps.json`: handoff `{gaps:[{id,statement,sub_problem,why_open,closing_evidence,closest_prior,evidence}]}`.
- `status.json`: completion status and `proposal_handoff_ready`.

Rerun the same command and output directory after interruption. Completed role
steps are revalidated and reused, including a finished output file written before
its completion checkpoint. Invalid partial JSON is quarantined and retried.
Failed roles never receive completion checkpoints. Corpus/metadata errors and
failed searches remain visible rather than being interpreted as evidence of absence.

`--search-limit` defaults to 8 results per query, `--expansion-limit` to 5 neighbors,
`--max-papers` to 100 initial paper records, and `--max-gaps` to 12 processed gaps.
Verification may add sources beyond the initial paper limit. Extraction uses batches
of 8 passages of at most 3500 characters; long source paragraphs are split into
adjacent exact-text passages. Programmatic `DiscoverySettings` exposes these defaults.
Gap discovery sees at most `--max-claims` (default 160) lexically selected claims, while every checked claim is retained on disk. Verification and assessment see at most `--context-passages` (default 48) selected passages, plus any required original supporting spans. This bound is soft only when required evidence itself exceeds it.

Run from a source checkout or editable installation. The repository’s explicit wheel package list was left unchanged under the spec’s restriction on changes outside this module; standalone wheel distribution needs a later packaging update.

Search and extraction costs depend on the supplied corpus; no live cost estimate
or agent evaluation was performed in implementation.

The handoff exports only open/partially addressed gaps and actual corpus L IDs,
with up to eight top-ranked gaps. The existing proposal parser requires 3–8 gaps.
Fewer supported gaps are written honestly with `proposal_handoff_ready=false`;
the module does not fabricate extra gaps to satisfy that minimum. Passing this
artifact into proposal mode is deliberately left for later integration.

## Reference designs and remaining limits

The operator's SGHA, IdeaScientist, AutoIdea and Good Question design descriptions
informed recurring-limitations analysis, method comparisons, provenance edges and
value axes. No matching reference repositories were available locally; no external
source code or prompts were copied and no external license compatibility is claimed.
See `src/research_discovery/REFERENCE_DESIGNS.md`.

This implementation has offline protocol tests, not a live scholarly-provider or
model validation. It does not implement OCR, authenticated/paywalled retrieval,
semantic proof of quote entailment, exhaustive systematic review, or a calibrated
research-value model. Publication dates and acceptance are not independently verified.
