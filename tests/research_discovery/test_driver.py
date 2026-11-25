from dataclasses import replace
import json
import pytest
from src.research_discovery.driver import run_discovery
from src.research_discovery.config import DiscoverySettings
from src.ideation.role_runner import RoleAgent
from src.ideation.literature.corpus import load_corpus


def setup(tmp_path):
    papers=tmp_path/'papers';papers.mkdir()
    for i in range(2):(papers/f'{i}.md').write_text(f'# Method {i}\n\n## Limitations\nFeedback remains unstable in long rollouts for method {i}.')
    review=tmp_path/'review.md';review.write_text('# Review\n\n## Findings\nFeedback remains unstable [@0; @1].')
    class Provider:
        def __init__(self):self.calls=[]
        def search(self,q,n):self.calls.append(q);return []
        def expand(self,p,n):return []
        def fulltext(self,p,out):return ''
    return papers,review,Provider()


def fake_roles(calls,*,kill=None,verdict='open',many=False):
    def role(name,prompt,**kwargs):
        if kill and name==kill:raise RuntimeError('interrupted')
        calls.append(name);data=json.loads(prompt.split('\nUNTRUSTED DATA\n',1)[1])
        if name=='extract':
            rows=[]
            for doc in data['documents']:
                if not doc['passages']:continue
                p=doc['passages'][0]
                rows.append(dict(kind='limitation',statement=p['text'],uncertainty='reported',evidence=[dict(source=doc['record']['id'],passage=p['id'],start=0,end=len(p['text']),quote=p['text'])]))
            return {'claims':rows}
        if name=='discover':
            ids=[c['id'] for c in data['claims']]
            rows=[dict(statement=s,sub_problem=s,basis='recurring_limitation',claim_ids=ids,
                       why_open=[dict(statement='Feedback may impose additional cost.',claim_ids=[])],closing_evidence='A controlled long-horizon comparison.')
                  for s in (['Stability under feedback','Observation uncertainty','Variable action latency'] if many else ['Stability under feedback'])]
            return {'gaps':rows}
        doc=next(d for d in data['documents'] if d['origin']=='paper');p=doc['passages'][0]
        e=dict(source=doc['record']['id'],passage=p['id'],start=0,end=len(p['text']),quote=p['text'])
        if name=='verify':
            return dict(verdict=verdict,rationale='A reported limitation remains a positive observation.',positive_evidence=[e],counter_evidence=[e] if verdict=='already_resolved' else [],resolving_sources=[e['source']] if verdict=='already_resolved' else [])
        if name=='assess':
            return {**{axis:dict(score=3,rationale='Qualitative judgment supported by source evidence.',evidence=[e]) for axis in ('importance','difficulty','impact','feasibility')},'uncertainty':'Transfer remains uncertain.'}
        raise AssertionError(name)
    return role


def args(tmp_path,role):
    papers,review,provider=setup(tmp_path)
    return dict(topic='Feedback policies',papers=[papers],manuscript=review,out_dir=tmp_path/'out',
                generator=RoleAgent('codex'),judge=RoleAgent('claude'),run_role_fn=role,provider=provider,
                settings=DiscoverySettings(search_limit=2,expansion_limit=0),log=lambda *a:None)


def test_end_to_end_sources_handoff_and_resume(tmp_path):
    from src.ideation.proposal.schema import parse_gaps
    calls=[];kw=args(tmp_path,fake_roles(calls,many=True));path=run_discovery(**kw)
    assert path.name=='report.md' and path.exists()
    out=kw['out_dir'];handoff=json.loads((out/'gaps.json').read_text())
    allowed={r.id for r in load_corpus(out/'corpus')}
    assert len(parse_gaps(handoff,allowed))==3
    structured=json.loads((out/'research_gaps.json').read_text())
    assert len(structured['gaps'])==3 and all(g['assessment'] for g in structured['gaps'])
    assert 'Hypothesis:' in path.read_text() and 'paragraph' in path.read_text()
    before=list(calls);searches=list(kw['provider'].calls)
    run_discovery(**kw)
    assert calls==before and kw['provider'].calls==searches
    assert json.loads((out/'status.json').read_text())['proposal_handoff_ready']


def test_killed_run_resumes_without_finished_roles(tmp_path):
    calls=[];kw=args(tmp_path,fake_roles(calls,kill='verify'))
    with pytest.raises(RuntimeError):run_discovery(**kw)
    before=list(calls)
    run_discovery(**{**kw,'run_role_fn':fake_roles(calls)})
    assert calls[:len(before)]==before and calls.count('discover')==1 and calls.count('extract')==3
    assert not json.loads((kw['out_dir']/'status.json').read_text())['proposal_handoff_ready']


@pytest.mark.parametrize('verdict',['already_resolved','uncertain'])
def test_unsupported_gaps_retained_but_not_ranked_or_exported(tmp_path,verdict):
    calls=[];kw=args(tmp_path,fake_roles(calls,verdict=verdict));run_discovery(**kw)
    out=kw['out_dir'];assert json.loads((out/'gaps.json').read_text())=={'gaps':[]}
    assert json.loads((out/'research_gaps.json').read_text())['gaps'][0]['verification']['verdict']==verdict
    assert 'assess' not in calls


def test_changed_inputs_and_output_inside_corpus_rejected(tmp_path):
    calls=[];kw=args(tmp_path,fake_roles(calls));run_discovery(**kw)
    with pytest.raises(ValueError):run_discovery(**{**kw,'topic':'Different topic'})
    with pytest.raises(ValueError):run_discovery(**{**kw,'out_dir':kw['papers'][0]/'nested'})


def test_offline_mode_preserves_uncertainty(tmp_path):
    calls=[];kw=args(tmp_path,fake_roles(calls));kw['settings']=replace(kw['settings'],offline=True)
    run_discovery(**kw)
    assert not kw['provider'].calls
    v=json.loads((kw['out_dir']/'research_gaps.json').read_text())['gaps'][0]['verification']
    assert v['verdict']=='uncertain' and v['uncertainty_notes']


def test_new_resolving_paper_is_in_final_corpus_and_removes_gap(tmp_path):
    from src.research_discovery.providers import Paper
    from src.research_discovery.ingest import make_record
    calls=[];kw=args(tmp_path,fake_roles(calls))
    record=make_record({'title':'Feedback resolves rollout instability','url':'https://example.org/solution',
                        'abstract':'A controlled comparison shows feedback stabilizes long rollouts.'})
    class Provider:
        def search(self,q,n):return [Paper(record)] if 'existing solution' in q else []
        def expand(self,*a):return []
        def fulltext(self,*a):return 'Feedback stabilizes long rollouts in a controlled comparison.'
    base=fake_roles(calls)
    def role(name,prompt,**kwargs):
        if name!='verify':return base(name,prompt,**kwargs)
        calls.append(name);data=json.loads(prompt.split('\nUNTRUSTED DATA\n')[1])
        doc=next(d for d in data['documents'] if d['record']['id']==record.id);p=doc['passages'][0]
        e=dict(source=record.id,passage=p['id'],start=0,end=len(p['text']),quote=p['text'])
        return dict(verdict='already_resolved',rationale='Controlled feedback study supplies the closing evidence.',
                    positive_evidence=[],counter_evidence=[e],resolving_sources=[record.id])
    run_discovery(**{**kw,'provider':Provider(),'run_role_fn':role})
    assert record.id in {r.id for r in load_corpus(kw['out_dir']/'corpus')}
    assert json.loads((kw['out_dir']/'gaps.json').read_text())=={'gaps':[]}
    assert 'Controlled feedback study' in (kw['out_dir']/'report.md').read_text()


def test_failed_countersearch_does_not_promote_positive_claim(tmp_path):
    calls=[];kw=args(tmp_path,fake_roles(calls))
    class Provider:
        def search(self,q,n):
            if 'existing solution' in q or 'counterevidence' in q:raise OSError('rate limited')
            return []
        def expand(self,*a):return []
        def fulltext(self,*a):return ''
    run_discovery(**{**kw,'provider':Provider()})
    assert 'assess' not in calls and 'rate limited' in (kw['out_dir']/'report.md').read_text()
    assert json.loads((kw['out_dir']/'gaps.json').read_text())=={'gaps':[]}


def test_handoff_strips_unknown_inline_citation_labels(tmp_path):
    calls=[];base=fake_roles(calls)
    def role(name,prompt,**kwargs):
        raw=base(name,prompt,**kwargs)
        if name=='discover':
            raw['gaps'][0]['statement']+=' [Lfabricated]'
            raw['gaps'][0]['why_open'][0]['statement']+=' [Lfabricated]'
            raw['gaps'][0]['closing_evidence']+=' [Lfabricated]'
        return raw
    kw=args(tmp_path,role);run_discovery(**kw)
    assert '[Lfabricated]' not in (kw['out_dir']/'gaps.json').read_text()
    assert '[Lfabricated]' not in (kw['out_dir']/'report.md').read_text()
