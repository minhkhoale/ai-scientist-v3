from src.ideation.pool import Candidate, Match, PoolStore


def candidate(id): return Candidate(id,1,'generate',(),{'title':id,'hypothesis':id},(),'')


def test_elo_replay_is_idempotent(tmp_path):
    store=PoolStore(tmp_path)
    for id in ['a','b']: store.add_candidate(candidate(id)); store.mark(id,'eligible','review')
    m=Match(1,0,'a','b','debate','a','wins'); store.add_match(m); store.add_match(m)
    state=store.load()
    assert state.elo=={'a':1216,'b':1184} and state.matches_played=={'a':1,'b':1}
    assert len((tmp_path/'matches.jsonl').read_text().splitlines())==1


def test_invalid_match_no_change(tmp_path):
    store=PoolStore(tmp_path)
    for id in ['a','b']: store.add_candidate(candidate(id))
    store.add_match(Match(1,0,'a','b','pairwise',None,'invalid'))
    assert store.load().elo=={'a':1200,'b':1200}


def test_eligible_excludes_executed_rejected_invalid(tmp_path):
    store=PoolStore(tmp_path)
    for id,status in [('a','executed'),('b','rejected'),('c','invalid'),('d','eligible')]:
        store.add_candidate(candidate(id)); store.mark(id,status,'test')
    assert [c.id for c in store.load().eligible()]==['d']
    # Replaying a generation step must never reset a terminal status.
    store.add_candidate(candidate('a')); assert store.load().status['a']=='executed'


def test_outcome_implies_executed_after_crash(tmp_path):
    from src.ideation.pool import Outcome
    store=PoolStore(tmp_path); store.add_candidate(candidate('a')); store.mark('a','eligible','review')
    store.add_outcome(Outcome('a',1,'attempt',1200,2,1,True,'not_refuted'))
    assert store.load().eligible()==[] and store.load().status['a']=='executed'
