from dataclasses import replace
import json
from pathlib import Path
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
    selected=run_tournament_round(**args)
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
