import pytest
from typing import get_args
from src.ideation.roles import (RoleName, RoleContext, render_role, parse_candidate, parse_review, parse_verdict, parse_feedback, RoleOutputError)


def bundle(title='Adaptive search'):
    return {'title':title,'hypothesis':'Restarts improve score','implementation_plan':['Add restarts'],
            'target_rubrics':[{'id':'score','type':'metric','description':'Improve score'},{'id':'restarts','type':'implementation','description':'Use restarts'}],
            'prediction':{'metric':'combined_score','expected_delta':.1,'confidence':.6},
            'falsifier':{'metric':'combined_score','refuted_if':'<=','threshold':1,'description':'Does not improve baseline'}}


@pytest.mark.parametrize('role',get_args(RoleName))
def test_render_every_role_has_no_leftover_placeholders(role):
    ctx=RoleContext(*(['context']*6),frozenset({'La'}),'feedback','reality')
    text=render_role(role,ctx,CANDIDATE='candidate',CANDIDATE_A='a',CANDIDATE_B='b',PARENTS='parents',OUTCOMES='outcomes')
    assert '{{' not in text and text.startswith('<!-- Adapted from Co-Scientist (Apache-2.0), config/prompts/')


def test_parse_candidate_requires_preregistration():
    b=bundle(); del b['prediction']
    with pytest.raises(RoleOutputError): parse_candidate(b,cycle=1,strategy='generate',allowed=frozenset())


def test_parse_candidate_drops_unknown_citations():
    b=bundle(); b['citations']=['La','unknown']
    assert parse_candidate(b,cycle=1,strategy='generate',allowed=frozenset({'La'})).citations==('La',)


def test_parse_candidate_id_is_deterministic():
    a=parse_candidate(bundle(),cycle=1,strategy='generate',allowed=frozenset())
    b=parse_candidate(bundle(),cycle=2,strategy='evolve',allowed=frozenset())
    assert a.id==b.id and a.id.startswith('cand_')


def test_parse_review_rejects_unknown_verdict():
    with pytest.raises(RoleOutputError): parse_review({'verdict':'maybe'},candidate_id='a',allowed=frozenset())


def test_parse_verdict_accepts_only_a_or_b():
    assert parse_verdict({'winner':'a','rationale':'Better'})=='a'
    for value in [None,{}, {'winner':'c'}, {'winner':'a','rationale':2}]:
        with pytest.raises(RoleOutputError): parse_verdict(value)


def test_feedback_schema():
    with pytest.raises(RoleOutputError): parse_feedback({'common_weaknesses':'bad'})
