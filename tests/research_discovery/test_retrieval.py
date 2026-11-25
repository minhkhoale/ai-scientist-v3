import json
import pytest
from src.research_discovery.artifacts import Steps
from src.research_discovery.providers import ScholarProvider, retrieve
from src.research_discovery.models import ValidationError
from src.ideation.role_runner import RoleAgent


def work(id='W1',title='A method',refs=None):
    return dict(id='https://openalex.org/'+id,title=title,publication_year=2025,
                abstract_inverted_index={'Unstable':[0],'rollouts':[1]},authorships=[],
                ids={'doi':'https://doi.org/10.1234/'+id},referenced_works=refs or [],
                best_oa_location={'pdf_url':'https://example.org/'+id+'.pdf'})


def test_real_provider_protocol_search_expansion_and_fulltext(tmp_path):
    calls=[]
    def http(url):
        calls.append(url)
        if url.endswith('.pdf'):return b'%PDF-content'
        if '/works/W2' in url:return json.dumps(work('W2','An existing solution')).encode()
        return json.dumps({'results':[work(refs=['https://openalex.org/W2'])]}).encode()
    provider=ScholarProvider(http=http,sleep=lambda _:None,pdf_text=lambda p:'Actual full-text finding.')
    papers=provider.search('diffusion',2)
    expanded=provider.expand(papers[0],2)
    assert any(p.record.title=='An existing solution' for p in expanded)
    assert provider.fulltext(papers[0],tmp_path)=='Actual full-text finding.'
    assert any('search=' in u for u in calls) and any('cites' in u for u in calls)


def test_identifier_lists_and_retrieval_resume(tmp_path):
    from src.research_discovery.providers import Paper
    from src.ideation.literature.sources import LiteratureRecord
    class Provider:
        def __init__(self):self.calls=[]
        def lookup(self,id):self.calls.append(id);return Paper(LiteratureRecord('Lseed','Seed',abstract='A limitation'))
        def search(self,query,limit):self.calls.append(query);return []
        def expand(self,paper,limit):return []
        def fulltext(self,paper,root):return ''
    ids=tmp_path/'ids.txt';ids.write_text('arxiv:2501.12345\ndoi:10.1234/test\n')
    review=tmp_path/'review.md';review.write_text('# R\n\nA limitation.')
    provider=Provider()
    first=retrieve(topic='Topic',papers=[ids],manuscript=review,out=tmp_path/'out',provider=provider)
    calls=list(provider.calls)
    second=retrieve(topic='Topic',papers=[ids],manuscript=review,out=tmp_path/'out',provider=provider)
    assert first==second and provider.calls==calls
    assert 'arxiv:2501.12345' in calls and 'doi:10.1234/test' in calls


def test_role_checkpoint_validates_before_completion_and_recovers(tmp_path):
    calls=[]
    def role(name,prompt,**kw):calls.append(name);return {'ok':True}
    steps=Steps(tmp_path,RoleAgent('codex'),role)
    def check(raw):
        if not raw.get('ok'):raise ValidationError('bad')
        return raw
    assert steps.run('extract','a','prompt',check)=={'ok':True}
    steps.run('extract','a','prompt',check)
    assert calls==['extract']
    with pytest.raises(ValidationError):steps.run('extract','a','changed prompt',check)
    bad=Steps(tmp_path/'bad',RoleAgent('codex'),lambda *a,**k:None)
    with pytest.raises(ValidationError):bad.run('extract','a','prompt',check)
    assert not (tmp_path/'bad/a/completed.json').exists()


def test_identifier_seed_fetches_available_fulltext(tmp_path):
    from src.research_discovery.providers import Paper
    from src.ideation.literature.sources import LiteratureRecord
    class Provider:
        def lookup(self,id):return Paper(LiteratureRecord('Lseed','Seed',abstract='Abstract',url='https://arxiv.org/abs/2501.12345'),'https://arxiv.org/pdf/2501.12345')
        def search(self,*a):return []
        def expand(self,*a):return []
        def fulltext(self,paper,out):return 'Full text: a concrete limitation.'
    review=tmp_path/'review.md';review.write_text('# R\n\nA claim.')
    docs=retrieve(topic='Topic',papers=['2501.12345'],manuscript=review,out=tmp_path/'out',provider=Provider())
    seed=next(d for d in docs if d.origin=='paper')
    assert seed.fulltext_status=='fulltext_pdf' and 'concrete limitation' in seed.passages[0].text
