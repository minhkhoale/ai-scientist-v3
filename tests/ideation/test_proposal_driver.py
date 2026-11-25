from dataclasses import replace
import json
import pytest
from src.ideation.proposal.driver import ProposalSettings, run_proposal, render_overview
from src.ideation.role_runner import RoleAgent
from src.ideation.round import RoundSettings
from tests.ideation.test_proposal_schema import proposal


def fake_proposal_roles():
    calls=[]
    def run(name,prompt,**kwargs):
        calls.append((name,kwargs['out_dir']))
        assert kwargs['container'] is None
        if name=='parse_goal':
            return dict(objective='Reduce drift',preferences=[],constraints=[],idea_attributes=[],
                        focus_axes=['Anchors','Training','Sampling'],domain_hint=None,compute_note='Small')
        if name=='codebase_scan': return dict(summary='Code',entrypoints=[],components=[],extension_points=[])
        if name.startswith('generate'):
            raw=proposal('Anchor memory' if name=='generate' else 'Temporal regularizer')
            raw['statement']='Retain memory' if name=='generate' else 'Optimize trajectory'
            return raw
        if name=='reflect': return dict(verdict='pass',summary='Good',citations=[])
        if name=='verify': return dict(assumptions=[dict(claim='Works',status='plausible',rationale='Evidence')],weakest='Works',kill_experiment='Ablate')
        if name.startswith('judge_'): return dict(winner='a',rationale='Better')
        if name=='metareview': return dict(common_weaknesses=[],common_strengths=[],focus_areas=[],narrative='Continue')
        if name=='overview': return {'markdown':'# Executive summary\n[ cand ] [cand_unknown] [Lunknown]'}
        raise AssertionError(name)
    return run,calls


def args(tmp_path,run):
    return dict(goal='Reduce drift',out_dir=tmp_path,corpus=[],codebase=None,
                generator=RoleAgent('codex'),judge=RoleAgent('claude'),run_role_fn=run,
                settings=ProposalSettings(rounds=4,stable_rounds=2,
                    round=RoundSettings(per_round=2,matches_per_round=0,metareview_every=1)),log=lambda *a:None)


def test_full_run_stability_and_resume(tmp_path):
    run,calls=fake_proposal_roles(); kw=args(tmp_path,run)
    path=run_proposal(**kw)
    assert path==tmp_path/'overview.md' and path.exists()
    assert (tmp_path/'plan.json').exists()
    rows=[json.loads(r) for r in (tmp_path/'proposals.jsonl').read_text().splitlines()]
    assert len(rows)==2 and rows[0]['verification']['kind']=='verification'
    status=json.loads((tmp_path/'status.json').read_text())
    assert status['state']=='stable' and status['rounds_run']==2
    files={p:p.read_bytes() for p in (tmp_path/'ideation').glob('*.jsonl')}
    before=list(calls); run_proposal(**kw)
    assert calls==before and all(p.read_bytes()==data for p,data in files.items())


def test_killed_round_resumes_without_duplicate_rows(tmp_path):
    run,calls=fake_proposal_roles(); kw=args(tmp_path,run)
    def kill(name,prompt,**kwargs):
        if name=='verify': raise RuntimeError('killed')
        return run(name,prompt,**kwargs)
    with pytest.raises(RuntimeError): run_proposal(**{**kw,'run_role_fn':kill})
    run_proposal(**kw)
    assert sum(name=='parse_goal' for name,_ in calls)==1
    assert sum(name=='reflect' for name,_ in calls)==2
    for name in ('candidates','reviews','matches'):
        path=tmp_path/'ideation'/f'{name}.jsonl'
        before=path.read_bytes() if path.exists() else b''
        run_proposal(**kw)
        assert (path.read_bytes() if path.exists() else b'')==before


def test_overview_filters_references():
    assert render_overview({'markdown':'[cand_ok] [cand_bad] [Lgood] [Lbad]'},
        candidate_ids={'cand_ok'},citation_ids={'Lgood'})=='[cand_ok]  [Lgood] '


def test_too_few_candidates_stops_cleanly(tmp_path):
    run,_=fake_proposal_roles(); kw=args(tmp_path,run)
    kw['settings']=replace(kw['settings'],round=RoundSettings(per_round=1,matches_per_round=0))
    run_proposal(**kw)
    assert json.loads((tmp_path/'status.json').read_text())['state']=='no_candidates'


def test_resume_after_snapshot_before_stop_decision(tmp_path, monkeypatch):
    from src.ideation.proposal import driver
    run,calls=fake_proposal_roles(); kw=args(tmp_path,run)
    write=driver._write
    def crash(path,value):
        if path.name=='status.json': raise RuntimeError('killed after snapshot')
        return write(path,value)
    monkeypatch.setattr(driver,'_write',crash)
    with pytest.raises(RuntimeError): run_proposal(**kw)
    monkeypatch.setattr(driver,'_write',write)
    run_proposal(**kw)
    assert json.loads((tmp_path/'status.json').read_text())['rounds_run']==2
    assert not any('0003' in str(path) for _,path in calls)


def test_role_script_accepts_every_role_output_file():
    import re
    from pathlib import Path
    from src.ideation.roles import OUTPUT_FILE
    script = (Path(__file__).parents[2] / 'src/scripts/ideation_role.sh').read_text()
    allowed = set(re.search(r'case "\$EXPECTED_OUTPUT" in ([^)]+)\)', script)[1].split('|'))
    assert set(OUTPUT_FILE.values()) <= allowed


def test_gaps_are_mapped_before_generation_and_ideas_target_them(tmp_path):
    from src.ideation.literature.sources import LiteratureRecord
    from tests.ideation.test_proposal_schema import gap
    base,calls=fake_proposal_roles(); prompts={}
    def run(name,prompt,**kwargs):
        prompts.setdefault(name,prompt)
        if name=='gap_analysis':
            calls.append((name,kwargs['out_dir']))
            return {'gaps':[gap(g,'L1','L2') for g in ('G1','G2','G3')]}
        raw=base(name,prompt,**kwargs)
        if name.startswith('generate'): raw['gap_id']='G2'
        return raw
    corpus=[LiteratureRecord('L1','One'),LiteratureRecord('L2','Two')]
    run_proposal(**{**args(tmp_path,run),'corpus':corpus})
    names=[n for n,_ in calls]
    assert names.index('gap_analysis') < names.index('generate')
    assert 'G1: No method does X' in prompts['generate'] and 'literature_gaps' in prompts['reflect']
    assert json.loads((tmp_path/'gaps.json').read_text())['gaps'][0]['id']=='G1'
    rows=[json.loads(r) for r in (tmp_path/'proposals.jsonl').read_text().splitlines()]
    assert rows and all(r['bundle']['gap_id']=='G2' for r in rows)
