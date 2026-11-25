from dataclasses import replace
from src.research_discovery.context import bounded_documents,select_claims
from src.research_discovery.ingest import passages
from tests.research_discovery.test_discovery import corpus


def test_required_quote_context_survives_the_bound():
    from src.research_discovery.models import Evidence
    docs,_=corpus();doc=docs[0]
    docs[0]=replace(doc,passages=passages('First.\n\nSecond.\n\nCritical finding.',doc.record.id))
    p=docs[0].passages[-1]
    required=Evidence(doc.record.id,p.id,0,len(p.text),p.text)
    result=bounded_documents(docs,'unrelated query',required=(required,),limit=1)
    assert sum(len(d.passages) for d in result)==1
    assert result[0].passages[0].id==p.id


def test_claim_selection_is_bounded_and_deterministic():
    _,claims=corpus()
    assert len(select_claims(claims,'rollouts',1))==1
    assert select_claims(claims,'rollouts',1)==select_claims(claims,'rollouts',1)
