from dataclasses import replace
import math
import pytest
from src.research_discovery.assessment import parse_assessment,rank_gaps
from src.research_discovery.discovery import discover_gaps
from src.research_discovery.models import ValidationError
from src.research_discovery.verification import parse_verification,SearchAudit
from tests.research_discovery.test_discovery import corpus,gap
from tests.research_discovery.test_verification import span,raw


def assessment(docs,score=4,difficulty=2):
    axes={name:dict(score=difficulty if name=='difficulty' else score,rationale='Supported qualitative judgment',evidence=[span(docs[0])])
          for name in ('importance','difficulty','impact','feasibility')}
    return {**axes,'uncertainty':'Scores are judgments; compute costs remain uncertain.'}


def test_rank_is_justified_and_difficulty_is_a_cost():
    docs,claims=corpus();g=discover_gaps({'gaps':[gap(claims)]},claims,docs).gaps[0]
    v=parse_verification(raw('open',docs),gap_id=g.id,documents=docs,searches=[SearchAudit('q','ok',())])
    a=parse_assessment(assessment(docs),gap_id=g.id,documents=docs)
    harder=parse_assessment(assessment(docs,difficulty=5),gap_id=g.id,documents=docs)
    first=rank_gaps([g],{g.id:v},{g.id:a})
    assert first[0].score>rank_gaps([g],{g.id:v},{g.id:harder})[0].score
    assert first[0].assessment.feasibility.evidence
    assert not rank_gaps([g],{g.id:replace(v,verdict='uncertain')},{g.id:a})
    assert not rank_gaps([g],{g.id:replace(v,verdict='already_resolved')},{g.id:a})


@pytest.mark.parametrize('score',[True,0,6,math.nan,math.inf,'4'])
def test_strict_scores(score):
    docs,_=corpus()
    with pytest.raises(ValidationError):parse_assessment(assessment(docs,score=score),gap_id='g',documents=docs)


def test_unsupported_rationale_evidence_rejected():
    docs,_=corpus();r=assessment(docs);r['impact']['evidence'][0]['source']='Lfake'
    with pytest.raises(ValidationError):parse_assessment(r,gap_id='g',documents=docs)


def test_ties_are_deterministic_and_partial_discount_visible():
    docs,claims=corpus();g=discover_gaps({'gaps':[gap(claims)]},claims,docs).gaps[0];other=replace(g,id='gap_000')
    a=parse_assessment(assessment(docs),gap_id=g.id,documents=docs)
    v=parse_verification(raw('partially_addressed',docs),gap_id=g.id,documents=docs,searches=[SearchAudit('q','ok',())])
    result=rank_gaps([g,other],{g.id:v,other.id:replace(v,gap_id=other.id)},
                     {g.id:a,other.id:replace(a,gap_id=other.id)})
    assert result[0].gap.id=='gap_000' and result[0].rank==1
    assert result[0].score==64.0
