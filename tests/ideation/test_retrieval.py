from src.ideation.literature.retrieval import Bm25Index, render_pack, pack_ids
from src.ideation.literature.sources import LiteratureRecord


def test_bm25_ranks_matching_abstract_first():
    a=LiteratureRecord('La','Unrelated',abstract='a robot diffusion policy')
    b=LiteratureRecord('Lb','Other',abstract='language model')
    assert Bm25Index([b,a]).search('diffusion policy',1)==[a]


def test_bm25_empty_query_returns_empty():
    assert Bm25Index([]).search('',8)==[]


def test_render_pack_truncates_and_labels():
    a=LiteratureRecord('La','Paper',abstract='abcdef',venue='ICLR',year=2025,url='https://a')
    pack=render_pack([a],max_abstract_chars=3)
    assert '### [La] Paper (ICLR 2025)' in pack and 'abc' in pack and 'abcdef' not in pack
    assert pack_ids([a])==frozenset({'La'})
