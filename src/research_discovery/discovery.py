"""Gap hypotheses backed by a checked, cross-paper claim graph.

Conceptual mapping: SGHA recurring evidence, IdeaScientist method contrasts,
AutoIdea claim-to-gap edges. No reference implementation code is copied.
"""
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
import re
from .models import (Evidence, ValidationError, UnsupportedEvidence, object_fields,
                     text, array, evidence, identifier)


@dataclass(frozen=True)
class Hypothesis:
    statement: str
    claim_ids: tuple[str, ...]
    label: str = 'hypothesis'


@dataclass(frozen=True)
class ResearchGap:
    id: str
    statement: str
    sub_problem: str
    basis: str
    claim_ids: tuple[str, ...]
    why_open: tuple[Hypothesis, ...]
    closing_evidence: str
    closest_prior: tuple[str, ...]
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class GapDiscovery:
    gaps: tuple[ResearchGap, ...]
    dropped: tuple[str, ...]


def discover_gaps(raw, claims, documents):
    object_fields(raw,('gaps',));by_id={c.id:c for c in claims};gaps=[];dropped=[]
    paper_ids={d.record.id for d in documents if d.origin=='paper'}
    canonical=lambda s:' '.join(re.findall(r'\w+',s.casefold()))
    for item in array(raw['gaps']):
        object_fields(item,('statement','sub_problem','basis','claim_ids','why_open','closing_evidence'))
        statement=text(item['statement']);sub_problem=text(item['sub_problem']);closing=text(item['closing_evidence'])
        if item['basis'] not in ('recurring_limitation','method_comparison','contradiction'):
            raise ValidationError('unknown gap basis')
        ids=tuple(text(c) for c in array(item['claim_ids']))
        if not ids or any(c not in by_id for c in ids):
            dropped.append('unknown or missing claim edges');continue
        try:
            spans=tuple(dict.fromkeys(evidence(asdict(e),documents) for id in ids for e in by_id[id].evidence))
            support={e.source for e in spans}&paper_ids
            if len(support)<2:raise UnsupportedEvidence('cross-paper gap needs two paper sources')
        except UnsupportedEvidence as exc:
            dropped.append(str(exc));continue
        hypotheses=[]
        for reason in array(item['why_open']):
            object_fields(reason,('statement','claim_ids'))
            refs=tuple(text(id) for id in array(reason['claim_ids']))
            if any(id not in ids for id in refs):raise ValidationError('reason cites an unsupported claim')
            # Even an explanation with supporting observations remains a causal hypothesis.
            hypotheses.append(Hypothesis(text(reason['statement']),refs))
        if not hypotheses:raise ValidationError('state at least one plausible unresolved-reason hypothesis')
        if any(canonical(g.sub_problem)==canonical(sub_problem) and
               SequenceMatcher(None,canonical(g.statement),canonical(statement)).ratio()>=.9 for g in gaps):
            dropped.append('duplicate gap');continue
        id=identifier('gap_',canonical(statement)+'\n'+canonical(sub_problem))
        gaps.append(ResearchGap(id,statement,sub_problem,item['basis'],ids,tuple(hypotheses),closing,
                                tuple(sorted(support)),spans))
    return GapDiscovery(tuple(gaps),tuple(dropped))
