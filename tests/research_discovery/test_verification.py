import pytest
from src.research_discovery.verification import parse_verification, SearchAudit, verify_gap
from src.research_discovery.discovery import discover_gaps
from src.research_discovery.artifacts import Steps
from src.research_discovery.config import DiscoverySettings
from src.research_discovery.models import ValidationError
from src.ideation.role_runner import RoleAgent
from tests.research_discovery.test_discovery import corpus,gap


def span(doc):
    p=doc.passages[0];return dict(source=doc.record.id,passage=p.id,start=0,end=len(p.text),quote=p.text)


def raw(verdict,docs):
    return dict(verdict=verdict,rationale='Evidence-grounded review',positive_evidence=[span(docs[0])],
                counter_evidence=[span(docs[1])],resolving_sources=[docs[1].record.id])


def test_resolved_requires_resolving_source_and_valid_quote():
    docs,_=corpus();r=raw('already_resolved',docs)
    v=parse_verification(r,gap_id='g',documents=docs,searches=[])
    assert v.verdict=='already_resolved' and v.resolving_sources==(docs[1].record.id,)
    with pytest.raises(ValidationError):parse_verification({**r,'resolving_sources':['Lfake']},gap_id='g',documents=docs,searches=[])
    with pytest.raises(ValidationError):parse_verification({**r,'counter_evidence':[]},gap_id='g',documents=docs,searches=[])


def test_empty_search_is_not_novelty_proof():
    docs,_=corpus();r={**raw('open',docs),'positive_evidence':[],'counter_evidence':[],'resolving_sources':[]}
    searches=[SearchAudit('query','ok',())]
    assert parse_verification(r,gap_id='g',documents=docs,searches=searches).verdict=='uncertain'
    assert parse_verification({**r,'positive_evidence':[span(docs[0])]},gap_id='g',documents=docs,searches=searches).verdict=='open'
    assert parse_verification({**r,'positive_evidence':[span(docs[0])]},gap_id='g',documents=docs,searches=[SearchAudit('q','failed',(), 'rate limited')]).verdict=='uncertain'


def test_partial_requires_both_sides_and_strict_labels():
    docs,_=corpus();r=raw('partially_addressed',docs)
    assert parse_verification(r,gap_id='g',documents=docs,searches=[SearchAudit('q','ok',())]).verdict=='partially_addressed'
    with pytest.raises(ValidationError):parse_verification({**r,'verdict':'novel'},gap_id='g',documents=docs,searches=[])


def test_independent_search_and_role_resume(tmp_path):
    from src.research_discovery.providers import Paper
    docs,claims=corpus();g=discover_gaps({'gaps':[gap(claims)]},claims,docs).gaps[0]
    class Provider:
        def __init__(self):self.calls=[]
        def search(self,q,n):self.calls.append(q);return [Paper(docs[1].record)]
        def fulltext(self,p,out):return ''
    provider=Provider();calls=[]
    def role(name,prompt,**kw):calls.append(name);return raw('already_resolved',docs)
    steps=Steps(tmp_path/'roles',RoleAgent('codex'),role)
    kw=dict(provider=provider,steps=steps,out=tmp_path,settings=DiscoverySettings())
    v,new=verify_gap(g,docs,**kw)
    assert v.verdict=='already_resolved' and len(provider.calls)==2
    verify_gap(g,docs,**kw)
    assert calls==['verify'] and len(provider.calls)==2 and len(new)==len(docs)
