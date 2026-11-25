"""Independent solution searches and conservative evidence-checked gap verdicts."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from .artifacts import digest, read_json, write_json
from .models import (Document, Evidence, ValidationError, object_fields, text, array,
                     evidence, document_from_dict)
from .ingest import passages
from .context import bounded_documents


@dataclass(frozen=True)
class SearchAudit:
    query: str
    status: str
    result_ids: tuple[str, ...]
    error: str = ''


@dataclass(frozen=True)
class Verification:
    gap_id: str
    verdict: str
    rationale: str
    positive_evidence: tuple[Evidence, ...]
    counter_evidence: tuple[Evidence, ...]
    resolving_sources: tuple[str, ...]
    searches: tuple[SearchAudit, ...]
    uncertainty_notes: tuple[str, ...] = ()


def parse_verification(raw, *, gap_id, documents, searches):
    object_fields(raw,('verdict','rationale','positive_evidence','counter_evidence','resolving_sources'))
    verdict=raw['verdict'];rationale=text(raw['rationale'])
    if verdict not in ('open','partially_addressed','already_resolved','uncertain'):
        raise ValidationError('unknown verification verdict')
    positive=tuple(evidence(e,documents) for e in array(raw['positive_evidence']))
    counter=tuple(evidence(e,documents) for e in array(raw['counter_evidence']))
    resolving=tuple(text(s) for s in array(raw['resolving_sources']))
    papers={d.record.id for d in documents if d.origin=='paper'}
    if any(s not in papers or s not in {e.source for e in counter} for s in resolving):
        raise ValidationError('resolving source needs a paper quote in counterevidence')
    notes=[]
    if verdict=='already_resolved' and (not resolving or not counter):
        raise ValidationError('resolution requires resolving paper evidence')
    if verdict in ('open','partially_addressed'):
        if not positive or not any(e.source in papers for e in positive):
            notes.append('No positive paper evidence supports an unresolved gap.')
        if not searches or any(s.status!='ok' for s in searches):
            notes.append('Independent search was unavailable or incomplete.')
        if verdict=='partially_addressed' and not counter:
            notes.append('Partial resolution requires counterevidence.')
        if notes:verdict='uncertain'
    return Verification(gap_id,verdict,rationale,positive,counter,resolving,tuple(searches),tuple(notes))


def counter_searches(gap,documents,*,provider,out,settings):
    queries=(gap.statement+' '+gap.sub_problem+' existing solution',
             gap.sub_problem+' '+gap.closing_evidence+' counterevidence')
    documents=list(documents);known={d.record.id for d in documents};audits=[]
    for query in queries:
        directory=Path(out)/'verification_searches'/gap.id/digest(query)
        completed=directory/'completed.json'
        if settings.offline or settings.search_limit==0:
            audits.append(SearchAudit(query,'skipped',(),'offline mode' if settings.offline else 'zero search budget'));continue
        if completed.exists():
            saved=read_json(completed)
            additions=[document_from_dict(d) for d in saved['documents']]
            ids=tuple(saved['ids'])
        else:
            try:
                papers=provider.search(query,settings.search_limit);additions=[];ids=[];errors=[]
                for paper in papers:
                    ids.append(paper.record.id)
                    if paper.record.id in known:continue
                    body=paper.record.abstract;status='abstract_only'
                    try:
                        full=provider.fulltext(paper,directory)
                        if full:body=full;status='fulltext_pdf'
                    except (OSError,ValueError) as exc:errors.append({'source':paper.record.id,'error':str(exc)})
                    additions.append(Document(paper.record,'paper',passages(body,paper.record.id,max_chars=settings.passage_chars),(),status))
                write_json(completed,{'query':query,'ids':ids,'documents':[asdict(d) for d in additions],'fulltext_errors':errors})
                ids=tuple(ids)
            except (OSError,ValueError,KeyError) as exc:
                audits.append(SearchAudit(query,'failed',(),str(exc)))
                write_json(directory/'failed_attempt.json',{'query':query,'error':str(exc)})
                continue
        for doc in additions:
            if doc.record.id not in known:documents.append(doc);known.add(doc.record.id)
        audits.append(SearchAudit(query,'ok',ids))
    return documents,tuple(audits)


def verify_gap(gap,documents,*,provider,steps,out,settings,judge=None):
    # Freeze search context before invoking the verifier. Even failed/empty searches
    # are part of that finished verification's evidence, not grounds to rerun it.
    context_path=Path(out)/'verification_contexts'/gap.id/'context.json'
    request=digest({'gap':asdict(gap),'settings':asdict(settings),'judge':asdict(judge or steps.agent)})
    if context_path.exists():
        saved=read_json(context_path)
        if saved['input_sha256']!=request:raise ValidationError('verification context inputs differ')
        by_id={d.record.id:d for d in documents}
        for raw in saved['documents']:
            doc=document_from_dict(raw)
            if doc.record.id not in by_id:by_id[doc.record.id]=doc
        documents=list(by_id.values())
        searches=tuple(SearchAudit(s['query'],s['status'],tuple(s['result_ids']),s['error']) for s in saved['searches'])
    else:
        documents,searches=counter_searches(gap,documents,provider=provider,out=out,settings=settings)
        write_json(context_path,{'input_sha256':request,'documents':[asdict(d) for d in documents],
                                 'searches':[asdict(s) for s in searches]})
    # The independent verifier sees the original support and new search evidence, not a generator verdict.
    ids=set(gap.closest_prior)|{id for search in searches for id in search.result_ids}
    relevant=bounded_documents([d for d in documents if d.record.id in ids],gap.statement+' '+gap.closing_evidence,
        required=gap.evidence,limit=settings.context_passages)
    prompt=(Path(__file__).parent/'prompts/verify.md').read_text()+'\nUNTRUSTED DATA\n'+json.dumps({
        'gap':asdict(gap),'searches':[asdict(s) for s in searches],
        'documents':[asdict(d) for d in relevant]},ensure_ascii=False)
    revision=digest({'gap':asdict(gap),'searches':[asdict(s) for s in searches]})[:12]
    result=steps.run('verify','verify_'+gap.id+'_'+revision,prompt,
        lambda raw:parse_verification(raw,gap_id=gap.id,documents=relevant,searches=searches),agent=judge)
    return result,documents
