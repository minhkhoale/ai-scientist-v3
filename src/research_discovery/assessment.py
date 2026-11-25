"""Evidence-backed value judgments and transparent deterministic ranking.

The four-axis decomposition follows the operator's Good Question design reference.
Weights are an explicit product heuristic, not an empirically validated value model.
"""
from dataclasses import dataclass
import math
from .models import Evidence, ValidationError, array, evidence, object_fields, text
from .discovery import ResearchGap
from .verification import Verification


@dataclass(frozen=True)
class Axis:
    score: float
    rationale: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class Assessment:
    gap_id: str
    importance: Axis
    difficulty: Axis
    impact: Axis
    feasibility: Axis
    uncertainty: str


@dataclass(frozen=True)
class RankedGap:
    rank: int
    score: float
    gap: ResearchGap
    verification: Verification
    assessment: Assessment


def parse_assessment(raw, *, gap_id, documents):
    object_fields(raw,('importance','difficulty','impact','feasibility','uncertainty'))
    axes={};papers={d.record.id for d in documents if d.origin=='paper'}
    for name in ('importance','difficulty','impact','feasibility'):
        obj=object_fields(raw[name],('score','rationale','evidence'))
        score=obj['score']
        if isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or not 1<=score<=5:
            raise ValidationError('score must be finite, numeric and in [1,5]')
        spans=tuple(evidence(e,documents) for e in array(obj['evidence']))
        if not spans or not any(e.source in papers for e in spans):
            raise ValidationError('each value judgment requires paper evidence')
        axes[name]=Axis(float(score),text(obj['rationale']),spans)
    return Assessment(gap_id,**axes,uncertainty=text(raw['uncertainty']))


def rank_gaps(gaps, verifications, assessments):
    scored=[]
    for gap in gaps:
        v=verifications.get(gap.id);a=assessments.get(gap.id)
        if v is None or a is None or v.verdict not in ('open','partially_addressed'):continue
        if a.gap_id!=gap.id or v.gap_id!=gap.id:raise ValidationError('assessment/verification gap mismatch')
        value=.3*a.importance.score+.3*a.impact.score+.25*a.feasibility.score+.15*(6-a.difficulty.score)
        score=round(20*value*(.8 if v.verdict=='partially_addressed' else 1),6)
        scored.append((score,gap,v,a))
    scored.sort(key=lambda row:(-row[0],row[1].id))
    return tuple(RankedGap(i,score,gap,v,a) for i,(score,gap,v,a) in enumerate(scored,1))
