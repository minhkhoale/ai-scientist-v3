from dataclasses import replace
import json
from types import SimpleNamespace
import pytest
from src.ideation.round import RoundSettings, run_tournament_round, make_tournament_idea_generation, record_outcome
from src.ideation.pool import PoolStore
from src.ideation.role_runner import RoleAgent
from src.ideation.roles import RoleContext
from src.investigation.idea_generation import load_idea_artifacts
from tests.ideation.test_roles import bundle


def fake_roles():
    calls=[]
    def role(role,prompt,**kwargs):
        calls.append(role)
        if role=='generate' or role.startswith('evolve_'):
            # Disjoint vocabulary keeps lexical dedup from obscuring round tests.
            b=bundle(f'Proposal {len(calls)}'); b['hypothesis']=f'Mechanism {len(calls)}'; return b
        if role=='reflect': return {'verdict':'pass','summary':'Feasible','citations':[]}
        if role.startswith('judge_'): return {'winner':'a','rationale':'Better'}
        return {'common_weaknesses':[],'common_strengths':[],'focus_areas':[],'narrative':'Continue'}
    return role,calls


def round_args(tmp_path,role):
    return dict(cycle=1,store=PoolStore(tmp_path/'ideation'),corpus=[],
                ctx_base=RoleContext('Optimize','','','','','',frozenset(),'',''),
                settings=RoundSettings(per_round=3,matches_per_round=6,dedup_threshold=1.0),
                generator=RoleAgent('codex'),judge=RoleAgent('claude'),run_role_fn=role,
                work_dir=tmp_path/'ideation',history_titles={},log_event_fn=lambda *a,**k:None)


def config():
    return SimpleNamespace(ideation=SimpleNamespace(per_round=3,matches_per_round=3,dedup_threshold=1.0,judge=SimpleNamespace(agent=None,model=None,reasoning_effort=None)))


def seam_args(tmp_path):
    task=tmp_path/'task'; task.mkdir(exist_ok=True); (task/'task.md').write_text('# Goal\nOptimize\n# Rubrics\nImprove')
    return dict(agent='codex',task_dir=task,results_dir=tmp_path,cycle_index=1,output_dir=tmp_path/'out')


def test_round_selects_top_elo_and_writes_bundle(tmp_path):
    role,calls=fake_roles()
    runner=make_tournament_idea_generation(config(),corpus_dir=None,run_role_fn=role,log_event_fn=lambda *a,**k:None)
    result=runner(**seam_args(tmp_path))
    assert result.exit_code==0
    artifacts=load_idea_artifacts(result.idea_dir,1,lambda p:json.loads(p.read_text()),require_preregistration=True)
    state=PoolStore(tmp_path/'ideation').load()
    assert artifacts.bundle['idea_id']==state.eligible()[0].id


def test_round_resume_skips_completed_matches(tmp_path):
    role,calls=fake_roles(); args=round_args(tmp_path,role)
    def interrupted(name,prompt,**kwargs):
        if name.startswith('judge_') and sum(n.startswith('judge_') for n in calls)==3: raise RuntimeError('crash')
        return role(name,prompt,**kwargs)
    args['run_role_fn']=interrupted
    with pytest.raises(RuntimeError): run_tournament_round(**args)
    args['run_role_fn']=role; run_tournament_round(**args)
    assert sum(n.startswith('judge_') for n in calls)==6
    before=list(calls); run_tournament_round(**args); assert calls==before


def test_round_generates_when_pool_exhausted(tmp_path):
    role,calls=fake_roles(); args=round_args(tmp_path,role)
    assert run_tournament_round(**args) is not None
    for c in args['store'].load().eligible(): args['store'].mark(c.id,'executed','test')
    args['cycle']=2; assert run_tournament_round(**args) is not None
    assert calls.count('generate')==6


def test_round_with_no_eligible_candidate_fails_cleanly(tmp_path):
    runner=make_tournament_idea_generation(config(),corpus_dir=None,run_role_fn=lambda *a,**k:None,log_event_fn=lambda *a,**k:None)
    result=runner(**seam_args(tmp_path)); assert result.exit_code==1 and result.output=='ideation_no_eligible_candidate'


def test_validation_feedback_picks_next_candidate(tmp_path):
    role,calls=fake_roles(); runner=make_tournament_idea_generation(config(),corpus_dir=None,run_role_fn=role,log_event_fn=lambda *a,**k:None)
    args=seam_args(tmp_path); runner(**args); first=json.loads((args['output_dir']/'idea_bundle.json').read_text())['idea_id']
    before=len(calls); assert runner(**args,validation_feedback='bad rubric').exit_code==0
    second=json.loads((args['output_dir']/'idea_bundle.json').read_text())['idea_id']
    assert first!=second and len(calls)==before and PoolStore(tmp_path/'ideation').load().status[first]=='invalid'


def test_evolution_children_have_parents_and_wait_a_round(tmp_path):
    role,calls=fake_roles(); args=round_args(tmp_path,role)
    args['settings']=replace(args['settings'],evolve_min_mature=1,evolve_mature_matches=0,evolve_strategies=('simplify',))
    selected=run_tournament_round(**args); state=args['store'].load()
    children=[c for c in state.candidates.values() if c.strategy=='simplify']
    assert len(children)==1 and children[0].parent_ids and state.status[children[0].id]=='draft'
    assert selected.id!=children[0].id


def test_record_outcome_marks_executed_and_feeds_reality(tmp_path):
    role,calls=fake_roles(); args=round_args(tmp_path,role); run_tournament_round(**args)
    for i,c in enumerate(args['store'].load().eligible()):
        record_outcome(tmp_path,idea_id=c.id,cycle=1,attempt_id=str(i),combined_score=i+1,baseline_score=0,attempt_pass=True,falsifier_outcome='not_refuted')
    assert len(args['store'].load().outcomes)==3
    seen=[]
    def capture(name,prompt,**kwargs): seen.append(prompt); return role(name,prompt,**kwargs)
    args.update(cycle=2,run_role_fn=capture); run_tournament_round(**args)
    assert 'spearman' in seen[0] and '"n": 3' in seen[0]


def test_judge_not_independent_logged_once(tmp_path):
    role,calls=fake_roles(); events=[]
    runner=make_tournament_idea_generation(config(),corpus_dir=None,run_role_fn=role,log_event_fn=lambda event,**k:events.append(event))
    args=seam_args(tmp_path); runner(**args); args['cycle_index']=2; runner(**args)
    assert events.count('ideation_judge_not_independent')==1


def test_failed_role_is_cached_on_resume(tmp_path):
    calls=[]
    def fail(role,prompt,**kwargs): calls.append(role); return None
    args=round_args(tmp_path,fail)
    assert run_tournament_round(**args) is None
    assert run_tournament_round(**args) is None
    assert calls==['generate']*3


def test_existing_executed_candidate_stays_executed(tmp_path):
    role,calls=fake_roles(); args=round_args(tmp_path,role); run_tournament_round(**args)
    state=args['store'].load(); candidate=next(iter(state.candidates.values()))
    args['store'].mark(candidate.id,'executed','test')
    def duplicate(role,prompt,**kwargs):
        if role=='generate': return dict(candidate.bundle)
        return {'verdict':'pass','summary':'ok','citations':[]}
    args.update(cycle=2,run_role_fn=duplicate)
    run_tournament_round(**args)
    assert args['store'].load().status[candidate.id]=='executed'


def test_reconcile_ledger_after_crash_before_pool_hook(tmp_path):
    role,calls=fake_roles(); args=round_args(tmp_path,role); chosen=run_tournament_round(**args)
    row={'idea_id':chosen.id,'cycle_index':1,'attempt_id':'a1','actual_score':2,'baseline_score':1,'attempt_pass':True,'falsifier_outcome':'not_refuted'}
    (tmp_path/'prediction_ledger.jsonl').write_text(json.dumps(row)+'\n')
    runner=make_tournament_idea_generation(config(),corpus_dir=None,run_role_fn=role,log_event_fn=lambda *a,**k:None)
    kw=seam_args(tmp_path); kw['cycle_index']=2; runner(**kw)
    state=args['store'].load()
    assert state.status[chosen.id]=='executed'
    assert state.outcomes[0].combined_score==2 and state.outcomes[0].attempt_pass


def test_duplicate_crash_before_status_is_rechecked(tmp_path,monkeypatch):
    original=bundle('Historical idea')
    def role(name,prompt,**kw):
        return original if name=='generate' else {'verdict':'pass','summary':'ok','citations':[]}
    args=round_args(tmp_path,role); args['history_titles']={'old':'Historical idea'}
    store=args['store']; mark=store.mark
    def crash(id,status,reason):
        if status=='rejected': raise RuntimeError('crash')
        mark(id,status,reason)
    monkeypatch.setattr(store,'mark',crash)
    with pytest.raises(RuntimeError): run_tournament_round(**args)
    monkeypatch.setattr(store,'mark',mark)
    assert run_tournament_round(**args) is None
    assert set(store.load().status.values())=={'rejected'}


def test_generator_prompts_differ_within_round(tmp_path):
    role, _ = fake_roles()
    prompts = []
    def recording(name, prompt, **kwargs):
        if name == 'generate':
            prompts.append(prompt)
        return role(name, prompt, **kwargs)
    args = round_args(tmp_path, recording)
    run_tournament_round(**args)
    titles = [c.bundle['title'] for c in args['store'].load().candidates.values()]
    assert len(set(prompts)) == 3
    assert all(title in prompts[2] for title in titles[:2])
