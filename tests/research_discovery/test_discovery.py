from dataclasses import replace
import pytest
from src.research_discovery.ingest import make_record, passages
from src.research_discovery.models import Document, ValidationError
from src.research_discovery.extraction import extract_claims
from src.research_discovery.discovery import discover_gaps


def corpus():
    docs=[];raw=[]
    for i,body in enumerate(('Long rollouts remain unstable.','Long rollouts need feedback.')):
        r=make_record({'title':f'Method {i}','url':f'https://example.org/{i}'})
        p=passages(body,r.id)[0];docs.append(Document(r,'paper',(p,)))
        raw.append(dict(kind='limitation',statement=body,uncertainty='reported',evidence=[dict(source=r.id,passage=p.id,start=0,end=len(body),quote=body)]))
    return docs,extract_claims({'claims':raw},docs).claims


def gap(claims):
    return dict(statement='Stabilize long rollouts.',sub_problem='Closed-loop stability',basis='recurring_limitation',
                claim_ids=[c.id for c in claims],why_open=[dict(statement='Feedback may be expensive.',claim_ids=[])],
                closing_evidence='Matched long-horizon stability evaluation with feedback controls.')


def test_distinct_grounded_gaps_and_hypothesis_labels():
    docs,claims=corpus();a=gap(claims);b={**a,'statement':'Explain conflicting feedback results.','sub_problem':'Contradiction','basis':'contradiction'}
    result=discover_gaps({'gaps':[a,b]},claims,docs)
    assert len(result.gaps)==2
    assert result.gaps[0].why_open[0].label=='hypothesis'
    assert set(result.gaps[0].closest_prior)=={d.record.id for d in docs}
    assert all(e.quote for g in result.gaps for e in g.evidence)


def test_duplicates_and_invented_claims_are_dropped():
    docs,claims=corpus();a=gap(claims)
    result=discover_gaps({'gaps':[a,{**a,'statement':'Stabilize long rollouts!'},
        {**a,'claim_ids':['invented']}]},claims,docs)
    assert len(result.gaps)==1 and len(result.dropped)==2


def test_manuscript_is_not_an_independent_second_paper():
    docs,claims=corpus();docs[1]=replace(docs[1],origin='manuscript')
    result=discover_gaps({'gaps':[gap(claims)]},claims,docs)
    assert not result.gaps


def test_strict_gap_schema_and_unsupported_causal_links():
    docs,claims=corpus();a=gap(claims)
    with pytest.raises(ValidationError):discover_gaps({'gaps':[{**a,'basis':'made_up'}]},claims,docs)
    with pytest.raises(ValidationError):discover_gaps({'gaps':[{**a,'why_open':[dict(statement='X causes Y',claim_ids=['unknown'])]}]},claims,docs)
