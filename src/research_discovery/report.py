"""Deterministic, provenance-grounded reports and proposal-compatible handoff."""
from dataclasses import asdict
import re
from src.ideation.literature.corpus import _atomic_write
from .artifacts import write_json

FORMULA='20 × (0.30 importance + 0.30 impact + 0.25 feasibility + 0.15 (6 − difficulty)); partial-addressing multiplier 0.8'


def write_report(out,*,topic,documents,claims,gaps,verifications,assessments,ranked,limitations=()):
    known={d.record.id for d in documents};papers={d.record.id for d in documents if d.origin=='paper'}
    claim_map={c.id:c for c in claims};ranks={r.gap.id:r for r in ranked}
    clean=lambda value:re.sub(r'\[(L[^\]\s]+)\]',lambda m:m[0] if m[1] in known else '',str(value)).replace('\n',' ')
    all_rows=[]
    for gap in gaps:
        v=verifications[gap.id];a=assessments.get(gap.id);ranking=ranks.get(gap.id)
        all_rows.append({**asdict(gap),'verification':asdict(v),'assessment':asdict(a) if a else None,
                         'rank':ranking.rank if ranking else None,'score':ranking.score if ranking else None})
    write_json(out/'research_gaps.json',{'gaps':all_rows})
    write_json(out/'ranked_gaps.json',{'formula':FORMULA,'gaps':[asdict(r) for r in ranked]})
    handoff=[]
    for row in ranked[:8]:
        gap=row.gap;findings=[];seen=set()
        for id in gap.claim_ids:
            claim=claim_map[id]
            for source in sorted({e.source for e in claim.evidence}&papers):
                if (source,claim.statement) not in seen:
                    findings.append({'citation':source,'finding':clean(claim.statement)});seen.add((source,claim.statement))
        handoff.append(dict(id=gap.id,statement=clean(gap.statement),sub_problem=clean(gap.sub_problem),
            why_open=' '.join('Unverified hypothesis: '+clean(h.statement) for h in gap.why_open),
            closing_evidence=clean(gap.closing_evidence),closest_prior=[s for s in gap.closest_prior if s in papers],evidence=findings))
    write_json(out/'gaps.json',{'gaps':handoff})
    lines=['# Research gap report','',clean(topic),'',
        f'Corpus: {len(papers)} scientific source records and {len(documents)-len(papers)} operator manuscript; {len(claims)} checked claims.',
        'Quotes and source identity are checked by code. Their scientific interpretation and relevance remain judgments, not entailment proofs.',
        'No missing search result proves novelty. Reasons a gap remains unresolved are hypotheses. Scores are uncalibrated research-value judgments.','',
        '## Ranked supported gaps','',FORMULA,'',
        '| Rank | Gap | Verdict | Score |','| --- | --- | --- | --- |']
    for row in ranked:lines.append(f'| {row.rank} | {clean(row.gap.statement).replace("|","/")} | {row.verification.verdict} | {row.score:.2f} |')
    if not ranked:lines+=['','No gap currently has sufficient verification and assessment for ranking.']
    for gap in gaps:
        v=verifications[gap.id];a=assessments.get(gap.id)
        lines+=['',f'## {gap.id}: {clean(gap.statement)}','',f'Sub-problem: {clean(gap.sub_problem)}. Basis: {gap.basis}.',
                f'Verification: **{v.verdict}** — {clean(v.rationale)}','']
        lines += ['Hypothesis: '+clean(h.statement)+(' (linked claims: '+', '.join(h.claim_ids)+')' if h.claim_ids else ' (no direct causal evidence)') for h in gap.why_open]
        lines+=['','Closing evidence: '+clean(gap.closing_evidence),'','### Supporting source passages','']
        for e in dict.fromkeys((*gap.evidence,*v.positive_evidence,*v.counter_evidence)):
            doc=next(d for d in documents if d.record.id==e.source);p=next(p for p in doc.passages if p.id==e.passage)
            lines += [f'[{e.source}] {clean(doc.record.title)} — {clean(p.section)}, paragraph {p.paragraph}, characters {e.start}:{e.end}; {doc.origin}; {doc.fulltext_status}.',
                      '> '+e.quote.replace('\n','\n> '),'']
        lines+=['### Independent searches','']
        for s in v.searches:lines.append(f'- {clean(s.query)} — {s.status}; results: '+(', '.join('['+id+']' for id in s.result_ids) or '(none)')+(' — '+clean(s.error) if s.error else ''))
        lines += ['- '+clean(n) for n in v.uncertainty_notes]
        if a:
            lines+=['','### Research value','']
            for name in ('importance','difficulty','impact','feasibility'):
                axis=getattr(a,name)
                lines.append(f'- {name}: {axis.score:g}/5 — {clean(axis.rationale)}; sources: '+', '.join('['+e.source+']' for e in axis.evidence))
            lines.append('Uncertainty: '+clean(a.uncertainty))
    lines+=['','## Limitations','',*('- '+clean(note) for note in limitations),
            '- Search, expansion and gap generation are bounded; undocumented solutions may exist.',
            '- A manuscript claim is evidence of what the operator wrote, not independent confirmation of the cited paper.',
            '- Full-text extraction requires pdftotext; unavailable full text remains explicitly abstract-only.',
            '- External reference implementations were not vendored or license-audited; design concepts follow the operator specification.',
            '- Proposal handoff requires 3–8 supported gaps; fewer gaps are exported honestly but marked not ready.','',
            '## Sources','']
    for d in sorted(documents,key=lambda d:d.record.id):
        lines.append(f'- [{d.record.id}] {clean(d.record.title)} — {d.record.url}; origin={d.origin}; {d.fulltext_status}')
    path=out/'report.md';_atomic_write(path,('\n'.join(lines)+'\n').encode())
    return path,len(handoff)
