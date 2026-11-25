import pytest
from src.ideation.pool import PoolStore
from src.ideation.roles import RoleOutputError
from src.ideation.proposal.schema import (normalize_proposal, parse_proposal_candidate,
    parse_plan, parse_codebase, parse_verification)


def proposal(title='Sparse memory'):
    return dict(title=title, statement='Selective memory reduces drift.', mechanism='Retain anchor frames',
                anticipated_outcomes='Less drift', novelty_argument='Selective anchors',
                falsifier={'description':'No drift reduction','measurement':'Frame error'},
                first_experiment={'description':'Ablation','scale':'Tiny','compute_estimate_gpu_hours':1,
                                  'success_criterion':'Lower error'}, codebase_touchpoints=[], risks=['Bias'],
                prediction={'confidence':.6}, citations=['Lgood','Lbad'])


def test_valid_proposal():
    raw=proposal()
    b=normalize_proposal(raw,cycle=1,codebase_root=None)
    assert b['idea_id'].startswith('cand_')
    c=parse_proposal_candidate(raw,cycle=1,strategy='literature',allowed={'Lgood'})
    assert c.citations==('Lgood',) and c.bundle['rejected_citations']==1
    assert c.id==parse_proposal_candidate(raw,cycle=2,strategy='debate',allowed=set()).id


@pytest.mark.parametrize('field', ['falsifier','first_experiment'])
def test_required_proposal_fields(field):
    raw=proposal(); del raw[field]
    with pytest.raises(RoleOutputError): normalize_proposal(raw,cycle=1,codebase_root=None)


def test_paths_stay_inside_root(tmp_path):
    (tmp_path/'model.py').touch()
    raw=proposal(); raw['codebase_touchpoints']=['../x','/etc/passwd','missing','model.py']
    (tmp_path/'escape').symlink_to('/etc/passwd')
    raw['codebase_touchpoints'].append('escape')
    b=normalize_proposal(raw,cycle=1,codebase_root=tmp_path)
    assert b['codebase_touchpoints']==['model.py'] and b['rejected_touchpoints']==4
    raw['codebase_touchpoints']=['../x']
    with pytest.raises(RoleOutputError): normalize_proposal(raw,cycle=1,codebase_root=tmp_path)


def test_verification_status_and_legacy_rows(tmp_path):
    with pytest.raises(RoleOutputError):
        parse_verification({'assumptions':[{'claim':'x','status':'maybe','rationale':'y'}]},candidate_id='c',allowed=set())
    store=PoolStore(tmp_path)
    store._append('reviews',dict(candidate_id='c',verdict='pass',summary='ok',citations=[],rejected_citations=0))
    assert store.load().reviews['c'].kind=='full'
    v=parse_verification({'assumptions':[{'claim':'x','status':'plausible','rationale':'y'}],
                          'weakest':'x','kill_experiment':'Test x'},candidate_id='c',allowed=set())
    store.add_review(v); store.add_review(v)
    assert store.load().verifications['c'].assumptions[0]['status']=='plausible'
    assert len(store._rows('reviews'))==2


def test_plan_and_scan(tmp_path):
    raw=dict(objective='Reduce drift',preferences=[],constraints=[],idea_attributes=[],
             focus_axes=['Memory','Training','Sampling'],domain_hint=None,compute_note='Small')
    assert parse_plan(raw)==raw
    with pytest.raises(RoleOutputError): parse_plan({**raw,'focus_axes':['one']})
    (tmp_path/'main.py').touch()
    scan=parse_codebase(dict(summary='Model',entrypoints=[{'path':'main.py','purpose':'Run'},
         {'path':'../bad','purpose':'bad'}],components=[],extension_points=[]),tmp_path)
    assert len(scan['entrypoints'])==1


def gap(gid, *cites):
    return dict(id=gid, statement='No method does X', sub_problem='credit', why_open='Needs Y',
                closing_evidence='Z improves', closest_prior=[cites[0], 'Lfake'],
                evidence=[{'citation':c,'finding':'Leaves X open'} for c in cites])


def test_gaps_need_two_supplied_papers():
    from src.ideation.proposal.schema import parse_gaps
    allowed={'L1','L2'}
    raw={'gaps':[gap('G1','L1','L2'),gap('G2','L1','L2'),gap('G3','L2','L1'),gap('G4','L1','Lfake'),gap('G1','L1','L2')]}
    gaps=parse_gaps(raw,allowed)
    assert [g['id'] for g in gaps]==['G1','G2','G3'] and gaps[0]['closest_prior']==['L1']
    raw['gaps']=raw['gaps'][:2]
    with pytest.raises(RoleOutputError): parse_gaps(raw,allowed)


def test_proposal_must_name_a_known_gap():
    raw=proposal()
    with pytest.raises(RoleOutputError): normalize_proposal(raw,cycle=1,codebase_root=None,gap_ids=frozenset({'G1'}))
    raw['gap_id']='G1'
    assert normalize_proposal(raw,cycle=1,codebase_root=None,gap_ids=frozenset({'G1'}))['gap_id']=='G1'
