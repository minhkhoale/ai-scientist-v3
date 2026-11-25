"""Resumable retrieve → extract → discover → verify → assess → report workflow."""
from dataclasses import asdict, replace
import json
from pathlib import Path
from src.ideation.role_runner import RoleAgent
from src.ideation.literature.retrieval import Bm25Index, render_pack
from src.ideation.literature.corpus import build_corpus
from .artifacts import Steps, native_role, write_json, read_json, digest, fingerprint_inputs
from .config import DiscoverySettings
from .context import select_claims, bounded_documents
from .models import ValidationError
from .providers import ScholarProvider, retrieve
from .extraction import extract_claims
from .discovery import discover_gaps
from .verification import verify_gap
from .assessment import parse_assessment, rank_gaps
from .report import write_report


def prompt(name,data):
    return (Path(__file__).parent/'prompts'/f'{name}.md').read_text()+'\nUNTRUSTED DATA\n'+json.dumps(data,ensure_ascii=False)


def run_discovery(*,topic,papers,manuscript,out_dir,generator=RoleAgent('codex'),judge=RoleAgent('claude'),
                  settings=DiscoverySettings(),provider=None,run_role_fn=native_role,sources=(),log=print):
    if not isinstance(topic,str) or not topic.strip():raise ValidationError('topic is required')
    manuscript=Path(manuscript).expanduser().resolve();out=Path(out_dir).expanduser().resolve()
    if not manuscript.is_file():raise ValidationError('manuscript does not exist')
    for item in papers:
        path=Path(item).expanduser()
        if path.is_dir() and out.is_relative_to(path.resolve()):raise ValidationError('output must be outside seed corpus directories')
    if out==manuscript:raise ValidationError('output cannot replace manuscript')
    inputs=dict(topic=topic,inputs=fingerprint_inputs(papers,manuscript),settings=asdict(settings),
                generator=asdict(generator),judge=asdict(judge),sources=list(sources))
    key=digest(inputs);saved=out/'inputs.json'
    if saved.exists():
        if read_json(saved)['sha256']!=key:raise ValidationError('run inputs differ; choose a new output directory')
    elif out.exists() and any(out.iterdir()):raise ValidationError('output is nonempty and is not a discovery run')
    out.mkdir(parents=True,exist_ok=True);write_json(saved,{'sha256':key,'inputs':inputs})
    status=out/'status.json'
    if status.exists() and read_json(status).get('state') in ('completed','no_supported_gaps'):
        if (out/'report.md').exists():return out/'report.md'
    provider=provider or ScholarProvider();steps=Steps(out/'steps',generator,run_role_fn);steps.timeout=settings.role_timeout
    write_json(status,{'state':'running','stage':'retrieval'})
    try:
        documents=retrieve(topic=topic,papers=papers,manuscript=manuscript,out=out,provider=provider,settings=settings,sources=sources)
        original_documents=list(documents);claims={};dropped=[]
        for document in original_documents:
            usable=[p for p in document.passages if p.section.casefold() not in ('references','bibliography')]
            for index in range(0,len(usable),settings.extraction_batch):
                part=replace(document,passages=tuple(usable[index:index+settings.extraction_batch]))
                data={'topic':topic,'documents':[asdict(part)]}
                extraction=steps.run('extract',f'extract_{document.record.id}_{index:05d}',prompt('extract',data),
                    lambda raw,part=part:extract_claims(raw,[part]))
                claims.update({c.id:c for c in extraction.claims});dropped.extend(extraction.dropped)
        claims=tuple(claims.values())
        write_json(out/'claims.json',{'claims':[asdict(c) for c in claims],'dropped':dropped})
        write_json(status,{'state':'running','stage':'discovery','claims':len(claims)})
        discovery_claims=select_claims(claims,topic,settings.max_discovery_claims)
        data={'topic':topic,'claims':[asdict(c) for c in discovery_claims],
              'literature_pack':render_pack(Bm25Index([d.record for d in documents if d.origin=='paper']).search(topic,12))}
        discovery=steps.run('discover','discover',prompt('discover',data),lambda raw:discover_gaps(raw,discovery_claims,documents))
        gaps=discovery.gaps[:settings.max_gaps]
        write_json(out/'candidate_gaps.json',{'gaps':[asdict(g) for g in discovery.gaps],'dropped':discovery.dropped})
        verifications={};assessments={}
        for gap in gaps:
            log('Verifying '+gap.id)
            write_json(status,{'state':'running','stage':'verification','gap':gap.id})
            v,documents=verify_gap(gap,documents,provider=provider,steps=steps,out=out,settings=settings,judge=judge)
            verifications[gap.id]=v
            if v.verdict in ('open','partially_addressed'):
                ids=set(gap.closest_prior)|{e.source for e in (*v.positive_evidence,*v.counter_evidence)}
                relevant=bounded_documents([d for d in documents if d.record.id in ids],topic+' '+gap.statement,
                    required=(*gap.evidence,*v.positive_evidence,*v.counter_evidence),limit=settings.context_passages)
                assessment_data={'topic':topic,'gap':asdict(gap),'verification':asdict(v),'documents':[asdict(d) for d in relevant]}
                a=steps.run('assess','assess_'+gap.id,prompt('assess',assessment_data),
                    lambda raw,gap=gap,relevant=relevant:parse_assessment(raw,gap_id=gap.id,documents=relevant),agent=judge)
                assessments[gap.id]=a
        ranked=rank_gaps(gaps,verifications,assessments)
        write_json(out/'documents.json',{'documents':[asdict(d) for d in documents]})
        records=[d.record for d in documents]
        build_corpus(out/'corpus',['folder:discovery-final-snapshot'],require_nonempty=True,fetchers={'folder':lambda _:records})
        audit=read_json(out/'retrieval/search_audit.json')
        limitations=[f"Retrieval issue: {item['step']}: {item['error']}" for item in audit['errors']]
        if len(discovery_claims)<len(claims):limitations.append(f'Gap discovery saw {len(discovery_claims)} of {len(claims)} claims under the lexical context bound; all checked claims remain in claims.json.')
        if audit['offline']:limitations.append('Offline mode: independent network verification was skipped; open claims remain uncertain.')
        if len(discovery.gaps)>len(gaps):limitations.append(f'Only {len(gaps)} of {len(discovery.gaps)} candidate gaps were processed under max_gaps.')
        if generator.agent==judge.agent and generator.model==judge.model:
            limitations.append('Verifier uses a fresh role session but the same agent/model as the generator; model diversity is absent.')
        report,handoff_count=write_report(out,topic=topic,documents=documents,claims=claims,gaps=gaps,
            verifications=verifications,assessments=assessments,ranked=ranked,limitations=limitations)
        write_json(status,{'state':'completed' if ranked else 'no_supported_gaps','claims':len(claims),
            'candidate_gaps':len(gaps),'ranked_gaps':len(ranked),'proposal_handoff_ready':3<=handoff_count<=8,
            'handoff_count':handoff_count,'report':str(report)})
        return report
    except Exception as exc:
        write_json(status,{'state':'failed','error':str(exc)})
        raise
