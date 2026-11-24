"""Resumable tournament rounds behind the existing idea-generation seam."""
from __future__ import annotations
from dataclasses import dataclass, asdict, fields, replace
import json
from pathlib import Path
import random
from typing import Callable, Sequence, Mapping
from src.investigation.models import IdeaGenerationResult
from src.investigation.harness_feedback import summarize_recent_attempts
from src.orchestrator import state as state_store
from .pool import Candidate, PoolStore, Match, Outcome
from .elo import select_pair, select_mode, reality_calibration
from .literature.sources import LiteratureRecord
from .literature.corpus import load_corpus, _atomic_write
from .literature.retrieval import Bm25Index, render_pack, pack_ids
from .similarity import find_duplicate, text_similarity
from .roles import RoleContext, RoleOutputError, render_role, parse_candidate, parse_review, parse_verdict, parse_feedback, OUTPUT_FILE
from .role_runner import RoleAgent, ContainerOptions, run_role


@dataclass(frozen=True)
class RoundSettings:
    per_round: int = 3
    matches_per_round: int = 6
    evolve_min_mature: int = 4
    evolve_mature_matches: int = 2
    evolve_top_k: int = 4
    evolve_strategies: tuple[str,...] = ('combine','simplify','out_of_box')
    metareview_every: int = 3
    pack_k_generate: int = 8
    pack_k_reflect: int = 6
    dedup_threshold: float = .85
    max_pool: int = 40
    seed: str = ''
    role_timeout_seconds: int = 1800


def _write_once(path: Path, value) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True,exist_ok=True)
        _atomic_write(path,(json.dumps(value,indent=2)+'\n').encode())


def _read(path: Path, default=None):
    try: return json.loads(path.read_text())
    except (OSError,ValueError): return default


def _rows(path: Path) -> list[dict]:
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _text(candidate: Candidate) -> str:
    return candidate.bundle['title']+' '+candidate.bundle['hypothesis']


def run_tournament_round(*, cycle: int, store: PoolStore, corpus: Sequence[LiteratureRecord],
                         ctx_base: RoleContext, settings: RoundSettings, generator: RoleAgent,
                         judge: RoleAgent, run_role_fn, work_dir: Path,
                         history_titles: Mapping[str,str], log_event_fn) -> Candidate | None:
    root = work_dir/'rounds'/f'{cycle:04d}'
    index = Bm25Index(corpus)
    state = store.load()
    for candidate in state.candidates.values():
        review = state.reviews.get(candidate.id)
        if candidate.cycle<cycle and state.status[candidate.id]=='draft' and review and review.verdict in ('pass','neutral'):
            store.mark(candidate.id,'eligible','evolution matured')
    feedback = [r for r in state.feedback if r.get('kind')=='metareview']
    context = replace(ctx_base,reality=json.dumps(reality_calibration(state.outcomes)),feedback=json.dumps(feedback[-3:]))

    def role(name, step, *, query='', **slots):
        directory = root/step
        completed = directory/'completed.json'
        if completed.exists(): return _read(completed)
        records = index.search(query or context.task_goal,settings.pack_k_generate if name=='generate' else settings.pack_k_reflect)
        ctx = replace(context,literature_pack=render_pack(records),allowed_citations=pack_ids(records))
        output = directory/OUTPUT_FILE[name]
        # An agent may have completed the file before the host was interrupted.
        raw = _read(output) if output.exists() else run_role_fn(name,render_role(name,ctx,**slots),out_dir=directory,
               agent=judge if name=='reflect' or name.startswith('judge_') else generator,
               timeout_seconds=settings.role_timeout_seconds,container=None)
        _write_once(completed,raw)
        return raw

    def ingest(raw, step, strategy, parents=()):
        decision = root/step/'decision.json'
        saved = _read(decision)
        if saved is not None: return store.load().candidates.get(saved.get('id')) if saved.get('accepted') else None
        try:
            records = index.search(context.task_goal,settings.pack_k_generate)
            candidate = parse_candidate(raw,cycle=cycle,strategy=strategy,allowed=pack_ids(records))
            if parents: candidate.parent_ids = tuple(parents)
        except RoleOutputError:
            _write_once(decision,{'accepted':False}); return None
        state = store.load()
        # Same-cycle persistence means a crash happened between append and decision.
        if candidate.id in state.candidates:
            accepted = state.candidates[candidate.id].cycle==cycle and state.status[candidate.id] not in ('executed','invalid','rejected','retired')
            _write_once(decision,{'id':candidate.id,'accepted':accepted})
            return state.candidates[candidate.id] if accepted else None
        existing = {id:_text(c) for id,c in state.candidates.items()}
        existing.update({'history:'+id:title for id,title in history_titles.items()})
        duplicate = find_duplicate(_text(candidate),existing,threshold=settings.dedup_threshold)
        # History has titles only; compare titles separately as well.
        duplicate = duplicate or find_duplicate(candidate.bundle['title'],history_titles,threshold=settings.dedup_threshold)
        store.add_candidate(candidate)
        if duplicate: store.mark(candidate.id,'rejected','duplicate:'+duplicate)
        _write_once(decision,{'id':candidate.id,'accepted':not bool(duplicate)})
        return candidate if not duplicate else None

    def reflect(candidate, step, child=False):
        state = store.load()
        if state.status[candidate.id] in ('executed','invalid','rejected','retired'): return
        review = state.reviews.get(candidate.id)
        if review is None:
            raw = role('reflect',step,query=_text(candidate),CANDIDATE=json.dumps(asdict(candidate)))
            try:
                review = parse_review(raw,candidate_id=candidate.id,allowed=pack_ids(index.search(_text(candidate),settings.pack_k_reflect)))
            except RoleOutputError:
                store.mark(candidate.id,'rejected','invalid review'); return
            store.add_review(review)
        if review.verdict not in ('pass','neutral'): store.mark(candidate.id,'rejected',review.verdict)
        elif not child: store.mark(candidate.id,'eligible',review.verdict)

    for i in range(settings.per_round):
        step = f'generate_{i:03d}'
        raw = role('generate',step)
        candidate = ingest(raw,step,'generate')
        if candidate: reflect(candidate,f'reflect_{candidate.id}')

    completed_matches = {(r['cycle'],r['match_index']) for r in store._rows('matches')}
    for i in range(settings.matches_per_round):
        if (cycle,i) in completed_matches: continue
        state = store.load(); eligible = state.eligible()
        texts = {c.id:_text(c) for c in eligible}
        pair_path = root/f'match_{i:03d}'/'pair.json'
        pair = _read(pair_path)
        if pair is None:
            ids = select_pair(eligible,state,random.Random(f'{settings.seed}:{cycle}:{i}'),lambda a,b:text_similarity(texts[a],texts[b],list(texts.values())))
            if ids is None: break
            pair = {'a':ids[0],'b':ids[1],'mode':select_mode(state,*ids)}
            _write_once(pair_path,pair)
        a,b,mode = pair['a'],pair['b'],pair['mode']
        def display(id): return json.dumps({'candidate':asdict(state.candidates[id]),'review':asdict(state.reviews[id]) if id in state.reviews else None})
        raw = role('judge_'+mode,f'match_{i:03d}',CANDIDATE_A=display(a),CANDIDATE_B=display(b))
        try: winner = parse_verdict(raw)
        except RoleOutputError: winner = None
        store.add_match(Match(cycle,i,a,b,mode,winner,str(raw.get('rationale','')) if isinstance(raw,dict) else 'invalid'))

    state = store.load()
    parents_path = root/'evolution_parents.json'
    parents = _read(parents_path)
    if parents is None:
        mature = [c for c in state.eligible() if state.matches_played[c.id]>=settings.evolve_mature_matches]
        parents = [c.id for c in mature[:settings.evolve_top_k]] if len(mature)>=settings.evolve_min_mature else []
        _write_once(parents_path,parents)
    if parents:
        for strategy in settings.evolve_strategies:
            step = 'evolve_'+strategy
            raw = role(step,step,PARENTS=json.dumps([{'candidate':asdict(state.candidates[id]),'review':asdict(state.reviews[id]) if id in state.reviews else None} for id in parents]))
            candidate = ingest(raw,step,strategy,parents)
            if candidate: reflect(candidate,'reflect_'+candidate.id,child=True)
    if settings.metareview_every>0 and cycle%settings.metareview_every==0:
        raw = role('metareview','metareview',OUTCOMES=json.dumps([asdict(o) for o in store.load().outcomes]))
        if not any(r.get('kind')=='metareview' and r.get('cycle')==cycle for r in store.load().feedback):
            try: store.add_feedback({'kind':'metareview','cycle':cycle,**parse_feedback(raw)})
            except RoleOutputError: pass
    eligible = store.load().eligible()
    for candidate in eligible[settings.max_pool:]: store.mark(candidate.id,'retired','pool cap')
    return eligible[0] if eligible else None


def make_tournament_idea_generation(cfg, *, corpus_dir: Path | None, run_role_fn=run_role, log_event_fn) -> Callable[...,IdeaGenerationResult]:
    corpus = load_corpus(corpus_dir) if corpus_dir else []
    def generate(**kwargs):
        results_dir = Path(kwargs['results_dir']); cycle = kwargs['cycle_index']; output_dir = Path(kwargs['output_dir'])
        store = PoolStore(results_dir/'ideation')
        selections = [r for r in store.load().feedback if r.get('kind')=='selection' and r['cycle']==cycle]
        if kwargs.get('validation_feedback'):
            if selections: store.mark(selections[-1]['candidate_id'],'invalid',kwargs['validation_feedback'])
            eligible = store.load().eligible(); candidate = eligible[0] if eligible else None
        elif selections:
            candidate = store.load().candidates.get(selections[-1]['candidate_id'])
        else:
            task_dir = Path(kwargs['task_dir'])
            task = (task_dir/'task.md').read_text()
            settings = RoundSettings(**{f.name:getattr(cfg.ideation,f.name) for f in fields(RoundSettings) if hasattr(cfg.ideation,f.name) and f.name!='seed'},seed=results_dir.name)
            generator = RoleAgent(kwargs['agent'],kwargs.get('agent_model'),kwargs.get('reasoning_effort'))
            jc = cfg.ideation.judge
            judge = RoleAgent(jc.agent or generator.agent,jc.model or generator.model,jc.reasoning_effort or generator.reasoning_effort)
            if (judge.agent,judge.model)==(generator.agent,generator.model) and not any(r.get('kind')=='judge_warning' for r in store.load().feedback):
                log_event_fn('ideation_judge_not_independent')
                store.add_feedback({'kind':'judge_warning'})
            container = ContainerOptions(**{f.name:kwargs[f.name] for f in fields(ContainerOptions) if f.name in kwargs})
            def invoke(*args,**role_kwargs):
                role_kwargs['container'] = container
                return run_role_fn(*args,**role_kwargs)
            harness = ''
            if kwargs.get('harness_evaluated_attempts'):
                harness = 'Implementation rubrics require verification objects: {"type":"source_contains","patterns":["literal code marker"]}. Include a metric rubric; the harness injects the combined_score gate. No prose verification strings.'
            ctx = RoleContext(task,task,harness,summarize_recent_attempts(task_dir,_rows(state_store.attempts_history_path(results_dir))),
                              json.dumps(_read(results_dir/state_store.BEST_MANIFEST_FILE,{})),'',frozenset(),'','')
            history = {str(i):r.get('title','') for i,r in enumerate(_rows(state_store.ideas_history_path(results_dir)))}
            candidate = run_tournament_round(cycle=cycle,store=store,corpus=corpus,ctx_base=ctx,settings=settings,generator=generator,judge=judge,
                        run_role_fn=invoke,work_dir=store.root,history_titles=history,log_event_fn=log_event_fn)
        if candidate is None: return IdeaGenerationResult('ideation_no_eligible_candidate',1,output_dir)
        state = store.load()
        if not selections or selections[-1]['candidate_id']!=candidate.id:
            store.add_feedback({'kind':'selection','cycle':cycle,'candidate_id':candidate.id,'elo':state.elo[candidate.id]})
        output_dir.mkdir(parents=True,exist_ok=True)
        (output_dir/'idea_bundle.json').write_text(json.dumps(candidate.bundle,indent=2)+'\n')
        (output_dir/'target_rubrics.json').write_text(json.dumps(candidate.bundle['target_rubrics'],indent=2)+'\n')
        summary = f'Tournament cycle {cycle}: selected {candidate.id} at Elo {state.elo[candidate.id]:.1f} from {len(state.eligible())} eligible candidates.'
        (output_dir/'wrapper_output.log').write_text(summary+'\n')
        return IdeaGenerationResult(summary,0,output_dir)
    return generate


def record_outcome(results_dir: Path, *, idea_id: str, cycle: int, attempt_id: str, combined_score: float | None,
                   baseline_score: float | None, attempt_pass: bool, falsifier_outcome: str) -> None:
    store = PoolStore(results_dir/'ideation'); state = store.load()
    if idea_id not in state.candidates: return
    selections = [r for r in state.feedback if r.get('kind')=='selection' and r.get('candidate_id')==idea_id and r.get('cycle')==cycle]
    elo = selections[-1]['elo'] if selections else state.elo[idea_id]
    store.add_outcome(Outcome(idea_id,cycle,attempt_id,elo,combined_score,baseline_score,attempt_pass,falsifier_outcome))
    store.mark(idea_id,'executed','attempt:'+attempt_id)
