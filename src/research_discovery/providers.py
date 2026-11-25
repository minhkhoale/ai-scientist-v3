"""Bounded scholarly retrieval with offline seams and durable source snapshots."""
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from src.ideation.literature.sources import (LiteratureRecord, http_get, fetch_arxiv,
    parse_source_spec, fetch_conference, HttpStatusError)
from src.ideation.literature.corpus import build_corpus
from .artifacts import write_json, read_json, fingerprint_inputs, digest
from .models import Document, ValidationError, document_from_dict
from .ingest import make_record, load_inputs, passages, pdf_text
from .config import DiscoverySettings


@dataclass(frozen=True)
class Paper:
    record: LiteratureRecord
    fulltext_url: str = ''
    references: tuple[str, ...] = ()
    provider_id: str = ''


class ScholarProvider:
    def __init__(self, *, http=http_get, sleep=time.sleep, pdf_text=pdf_text):
        self.http,self.sleep,self.pdf_text = http,sleep,pdf_text
        self.last=0.0

    def get(self,url):
        # Host research utilities, never role agents, perform literature network access.
        if url.startswith('http://export.arxiv.org'):url=url.replace('http://','https://',1)
        wait=.25-(time.monotonic()-self.last)
        if wait>0:self.sleep(wait)
        for attempt in range(3):
            self.last=time.monotonic()
            try:return self.http(url)
            except HttpStatusError as exc:
                if exc.status not in (429,500,502,503,504) or attempt==2:raise
                self.sleep(2**(attempt+1))
        raise ValidationError('unreachable retrieval state')

    def work(self,raw):
        title=raw.get('title') or raw.get('display_name')
        if not isinstance(title,str) or not title.strip():raise ValidationError('scholarly record lacks title')
        inverted=raw.get('abstract_inverted_index') or {};words={}
        for token,positions in inverted.items():
            for p in positions:words[p]=token
        doi=(raw.get('ids') or {}).get('doi') or raw.get('doi')
        record=make_record(dict(title=title,abstract=' '.join(words[k] for k in sorted(words)),
            authors=[a['author']['display_name'] for a in raw.get('authorships',[]) if a.get('author',{}).get('display_name')],
            year=raw.get('publication_year'),url=doi or raw.get('id','')),'openalex')
        location=raw.get('best_oa_location') or {}
        pdf=location.get('pdf_url') or ''
        if not pdf:
            pdf=next((loc.get('pdf_url') for loc in raw.get('locations',[]) if loc.get('is_oa') and loc.get('pdf_url')),'')
        return Paper(record,pdf,tuple(raw.get('referenced_works',[])),raw.get('id',''))

    def search(self,query,limit):
        if limit<=0:return []
        url='https://api.openalex.org/works?'+urllib.parse.urlencode({'search':query,'per-page':min(limit,200)})
        return [self.work(r) for r in json.loads(self.get(url))['results'][:limit]]

    def lookup(self,value):
        value=value.strip()
        aid=re.fullmatch(r'(?:arxiv:|https?://arxiv.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5}(?:v\d+)?)',value)
        if aid:
            raw=self.get('https://export.arxiv.org/api/query?'+urllib.parse.urlencode({'id_list':aid[1]}))
            ns={'a':'http://www.w3.org/2005/Atom'};entry=ET.fromstring(raw).find('a:entry',ns)
            if entry is None:raise ValidationError('arXiv identifier not found')
            def field(key):return entry.findtext('a:'+key,default='',namespaces=ns).strip()
            record=make_record(dict(title=field('title'),abstract=field('summary'),url=field('id'),year=field('published')[:4],authors=[n.findtext('a:name',namespaces=ns) for n in entry.findall('a:author',ns)]),'arxiv')
            return Paper(record,'https://arxiv.org/pdf/'+aid[1])
        value=value.removeprefix('doi:')
        if value.startswith('10.'):value='https://doi.org/'+value
        if not value.startswith(('https://doi.org/','https://openalex.org/')):
            raise ValidationError('expected arXiv id, DOI or OpenAlex work id')
        suffix=value.rsplit('/',1)[-1] if value.startswith('https://openalex.org/') else value
        return self.work(json.loads(self.get('https://api.openalex.org/works/'+suffix)))

    def expand(self,paper,limit):
        if limit<=0:return []
        if not paper.provider_id:
            matches=self.search(paper.record.title,3)
            normalized=lambda s:re.sub(r'\W','',s.casefold())
            paper=next((p for p in matches if normalized(p.record.title)==normalized(paper.record.title)),paper)
        result=[self.lookup(ref) for ref in paper.references[:limit]]
        if paper.provider_id and len(result)<limit:
            url='https://api.openalex.org/works?'+urllib.parse.urlencode({'filter':'cites:'+paper.provider_id.rsplit('/',1)[-1],'per-page':min(limit-len(result),200)})
            result.extend(self.work(r) for r in json.loads(self.get(url))['results'])
        return result[:limit]

    def fulltext(self,paper,out):
        url=paper.fulltext_url or ('https://arxiv.org/pdf/'+paper.record.arxiv_id if paper.record.arxiv_id else '')
        if not url:return ''
        if not url.startswith('https://'):raise ValidationError('full-text URL must be HTTPS')
        path=Path(out)/'pdfs'/(paper.record.id+'.pdf');path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():
            data=self.get(url)
            if not data.startswith(b'%PDF-'):raise ValidationError('full-text endpoint did not return PDF')
            from src.ideation.literature.corpus import _atomic_write
            _atomic_write(path,data)
        return self.pdf_text(path)


def retrieve(*,topic,papers,manuscript,out,provider=None,settings=DiscoverySettings(),sources=()):
    out=Path(out);provider=provider or ScholarProvider()
    key=digest(dict(topic=topic,inputs=fingerprint_inputs(papers,manuscript),settings=asdict(settings),sources=list(sources)))
    completed=out/'retrieval/completed.json'
    if completed.exists():
        saved=read_json(completed)
        if saved['input_sha256']!=key:raise ValidationError('retrieval inputs differ from saved run')
        return [document_from_dict(d) for d in saved['documents']]
    def cached(step,fn):
        path=out/'retrieval'/step/'completed.json'
        request=out/'retrieval/inputs.json'
        if request.exists() and read_json(request)['input_sha256']!=key:raise ValidationError('retrieval inputs differ')
        write_json(request,{'input_sha256':key})
        if path.exists():return read_json(path)
        result=fn();write_json(path,result);return result
    looked_up={}
    def lookup(value):
        def fetch_lookup():
            if settings.offline:raise ValidationError('offline mode requires local paper metadata')
            return asdict(provider.lookup(value))
        raw=cached('lookup_'+digest(value),fetch_lookup)
        paper=paper_from_dict(raw)
        looked_up[(paper.record.title,paper.record.url)]=paper
        return paper
    errors=[];derived=[]
    review_path=Path(manuscript)
    review_text=pdf_text(review_path) if review_path.suffix.lower()=='.pdf' else review_path.read_text()
    identifiers=['doi:'+id.rstrip('.,;]') for id in re.findall(r'(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s<>"\]]+)',review_text,re.I)]
    identifiers+=['arxiv:'+id for id in re.findall(r'(?:arxiv:\s*|https?://arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5}(?:v\d+)?)',review_text,re.I)]
    if not settings.offline:
        for value in dict.fromkeys(identifiers):
            while value.endswith(')') and value.count(')')>value.count('('):value=value[:-1]
            try:lookup(value);derived.append(value)
            except (OSError,ValueError,KeyError,ET.ParseError) as exc:errors.append({'step':'manuscript_reference','error':str(exc)})
    docs=load_inputs([*papers,*derived],manuscript,lookup=lookup,max_chars=settings.passage_chars)
    seen={d.record.id for d in docs};additional=[]
    def run_query(name,fn):
        try:return [paper_from_dict(p) for p in cached(name,lambda:[asdict(p) for p in fn()])]
        except (OSError,ValueError,KeyError,ET.ParseError) as exc:
            errors.append({'step':name,'error':str(exc)});return []
    if not settings.offline:
        additional+=run_query('topic_search',lambda:provider.search(topic,settings.search_limit))
        # Reuse existing source grammar/provider implementations for explicit sources.
        for raw in sources:
            spec=parse_source_spec(raw)
            if spec.kind=='folder':raise ValidationError('use papers for local folders')
            def fetch(spec=spec):
                fn=fetch_arxiv if spec.kind=='arxiv' else fetch_conference
                return [Paper(r) for r in fn(spec,http_get=provider.get,sleep=provider.sleep)]
            additional+=run_query('source_'+digest(raw),fetch)
        seeds=[Paper(d.record) for d in docs if d.origin=='paper']+list(additional)
        for p in seeds[:settings.max_papers]:
            additional+=run_query('expand_'+p.record.id,lambda p=p:provider.expand(p,settings.expansion_limit))
    for paper in additional:
        if paper.record.id in seen or len([d for d in docs if d.origin=='paper'])>=settings.max_papers:continue
        seen.add(paper.record.id);body=paper.record.abstract;status='abstract_only'
        try:
            full=cached('fulltext_'+paper.record.id,lambda:provider.fulltext(paper,out))
            if full:body=full;status='fulltext_pdf'
        except (OSError,ValueError) as exc:errors.append({'step':'fulltext_'+paper.record.id,'error':str(exc)})
        docs.append(Document(paper.record,'paper',passages(body,paper.record.id,max_chars=settings.passage_chars),(),status))
    if not settings.offline:
        hints={(p.record.title,p.record.url):p for p in additional}
        hints.update(looked_up)
        for i,doc in enumerate(docs):
            if doc.origin!='paper' or doc.fulltext_status!='abstract_only':continue
            paper=hints.get((doc.record.title,doc.record.url),Paper(doc.record))
            try:
                full=cached('fulltext_'+doc.record.id,lambda paper=paper:provider.fulltext(paper,out))
                if full:docs[i]=replace(doc,passages=passages(full,doc.record.id,max_chars=settings.passage_chars),fulltext_status='fulltext_pdf')
            except (OSError,ValueError) as exc:errors.append({'step':'fulltext_'+doc.record.id,'error':str(exc)})
    if len([d for d in docs if d.origin=='paper'])>settings.max_papers:
        raise ValidationError('seed corpus exceeds max_papers; increase the explicit limit')
    # Reuse the immutable corpus writer, including its checksum and loader contract.
    rows=[d.record for d in docs]
    build_corpus(out/'literature',['folder:discovery-snapshot'],require_nonempty=True,fetchers={'folder':lambda _:rows})
    write_json(out/'retrieval/search_audit.json',{'errors':errors,'offline':settings.offline,'sources':list(sources)})
    write_json(completed,{'input_sha256':key,'documents':[asdict(d) for d in docs]})
    return docs


def paper_from_dict(raw):
    record=dict(raw['record']);record['authors']=tuple(record['authors'])
    return Paper(LiteratureRecord(**record),raw.get('fulltext_url',''),tuple(raw.get('references',[])),raw.get('provider_id',''))
