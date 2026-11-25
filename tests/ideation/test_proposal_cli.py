import json
import pytest
from src.ideation.proposal import cli
from src.ideation.round import make_tournament_idea_generation, record_outcome
from src.ideation.pool import PoolStore
from tests.ideation.test_round import config, seam_args, fake_roles
from tests.ideation.test_roles import bundle


def test_argparse_round_trip():
    ns=cli.build_parser().parse_args(['run','--goal','Reduce drift','--source','folder:/papers',
        '--codebase','/code','--agent','codex','--model','test','--judge-agent','claude',
        '--rounds','2','--per-round','3','--matches-per-round','1','--out','/out'])
    assert ns.goal=='Reduce drift' and ns.source==['folder:/papers'] and ns.rounds==2


def test_select_preserves_order_and_rejects_unknown(tmp_path):
    (tmp_path/'proposals.jsonl').write_text(''.join(json.dumps({'id':id,'bundle':{'title':id}})+'\n' for id in ('cand_a','cand_b')))
    assert cli.main(['select',str(tmp_path),'cand_b','cand_a'])==0
    assert [json.loads(r)['id'] for r in (tmp_path/'selected.jsonl').read_text().splitlines()]==['cand_b','cand_a']
    before=(tmp_path/'selected.jsonl').read_bytes()
    assert cli.main(['select',str(tmp_path),'cand_unknown'])==2
    assert (tmp_path/'selected.jsonl').read_bytes()==before


def test_seeded_execution_converts_and_selects_before_generation(tmp_path):
    seeds=tmp_path/'selected.jsonl'
    seeds.write_text(''.join(json.dumps({'id':id,'bundle':{'title':id}})+'\n' for id in ('cand_seed_b','cand_seed_a')))
    cfg=config(); cfg.ideation.seed_proposals=str(seeds)
    calls=[]; base,_=fake_roles()
    def run(name,prompt,**kw):
        calls.append((name,prompt))
        if name=='concretize':
            raw=bundle('B seed' if 'cand_seed_b' in prompt else 'A seed')
            return raw
        return base(name,prompt,**kw)
    runner=make_tournament_idea_generation(cfg,corpus_dir=None,run_role_fn=run,log_event_fn=lambda *a,**k:None)
    args=seam_args(tmp_path)
    for cycle,seed_id in enumerate(('cand_seed_b','cand_seed_a'),1):
        args['cycle_index']=cycle
        assert runner(**args).exit_code==0
        b=json.loads((args['output_dir']/'idea_bundle.json').read_text())
        state=PoolStore(tmp_path/'ideation').load()
        assert state.candidates[b['idea_id']].strategy=='proposal:'+seed_id
        before=list(calls); runner(**args); assert calls==before
        record_outcome(tmp_path,idea_id=b['idea_id'],cycle=cycle,attempt_id=str(cycle),combined_score=1,
                       baseline_score=0,attempt_pass=True,falsifier_outcome='not_refuted')
    assert [name for name,_ in calls]==['concretize','reflect','concretize','reflect']
    args['cycle_index']=3; runner(**args)
    assert any(name=='generate' for name,_ in calls)


def test_seed_config_requires_coscientist():
    from src.investigation.config import InvestigationConfig
    from src.investigation.config_loader import _validate_ideation
    cfg=InvestigationConfig(); cfg.ideation.seed_proposals='selected.jsonl'
    with pytest.raises(ValueError,match='coscientist'): _validate_ideation(cfg)


def test_cli_run_uses_frozen_corpus_and_dispatch(tmp_path,monkeypatch,capsys):
    from src.agsci.main import main
    (tmp_path/'literature').mkdir()
    monkeypatch.setattr(cli,'load_corpus',lambda path:[])
    monkeypatch.setattr(cli,'build_corpus',lambda *a,**k:pytest.fail('existing corpus rebuilt'))
    def run(**kwargs):
        assert kwargs['goal']=='Goal' and kwargs['generator'].model=='test'
        (tmp_path/'proposals.jsonl').write_text(json.dumps({'id':'cand_a','elo':1200,'bundle':{'title':'A'}})+'\n')
        (tmp_path/'overview.md').write_text('Overview')
        return tmp_path/'overview.md'
    monkeypatch.setattr(cli,'run_proposal',run)
    assert main(['propose','run','--goal','Goal','--source','folder:/unused','--out',str(tmp_path),'--model','test'])==0
    assert 'cand_a' in capsys.readouterr().out


def test_seed_resume_after_candidate_append(tmp_path):
    seeds=tmp_path/'selected.jsonl'
    seeds.write_text(''.join(json.dumps({'id':id,'bundle':{'title':id}})+'\n' for id in ('cand_first','cand_second')))
    cfg=config(); cfg.ideation.seed_proposals=str(seeds)
    calls=[]; killed=False
    def run(name,prompt,**kw):
        nonlocal killed
        calls.append(name)
        if name=='concretize': return bundle('First' if 'cand_first' in prompt else 'Second')
        if name=='reflect' and not killed:
            killed=True
            raise RuntimeError('killed before review')
        return {'verdict':'pass','summary':'Good','citations':[]}
    runner=make_tournament_idea_generation(cfg,corpus_dir=None,run_role_fn=run,log_event_fn=lambda *a,**k:None)
    args=seam_args(tmp_path)
    with pytest.raises(RuntimeError): runner(**args)
    assert runner(**args).exit_code==0
    state=PoolStore(tmp_path/'ideation').load()
    assert len(state.candidates)==1
    assert state.eligible()[0].strategy=='proposal:cand_first'
    assert calls.count('concretize')==1
