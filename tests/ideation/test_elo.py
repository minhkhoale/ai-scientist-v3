import random
import pytest
from src.ideation.elo import update, select_pair, select_mode, reality_calibration
from src.ideation.pool import PoolState, Candidate, Outcome


def test_update_matches_reference_values():
    assert update(1200,1200,'a',0)==(1216,1184)
    assert update(1200,1200,'a',6)==(1208,1192)


def state():
    s=PoolState()
    for id in ['a','b','c','d']:
        s.candidates[id]=Candidate(id,1,'generate',(),{'title':id,'hypothesis':id},(),'')
        s.status[id]='eligible'; s.elo[id]=1200; s.matches_played[id]=4
    return s


def test_select_pair_seeded_is_deterministic():
    s=state()
    assert select_pair(s.eligible(),s,random.Random(3),lambda a,b:0)==select_pair(s.eligible(),s,random.Random(3),lambda a,b:0)


def test_select_pair_prefers_distant_ideas():
    s=state(); counts={'close':0,'far':0}
    for seed in range(200):
        pair=select_pair(s.eligible(),s,random.Random(seed),lambda a,b:1 if set([a,b])=={'c','d'} else 0)
        if set(pair)=={'c','d'}: counts['close']+=1
        if set(pair)=={'a','b'}: counts['far']+=1
    assert counts['far']>counts['close']


def test_select_mode_rules():
    s=state(); assert select_mode(s,'a','b')=='debate'
    s.elo['a']=1300; assert select_mode(s,'a','b')=='pairwise'
    s.matches_played['a']=1; assert select_mode(s,'a','b')=='debate'


def test_reality_calibration_spearman():
    rows=[Outcome(str(i),i,str(i),1200+i,i,0,True,'survived') for i in range(3)]
    assert reality_calibration(rows)=={'n':3,'spearman':pytest.approx(1)}
    assert reality_calibration(rows[:2])['spearman'] is None
    for row in rows: row.combined_score=1
    assert reality_calibration(rows)['spearman'] is None
