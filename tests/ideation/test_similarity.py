import pytest
from src.ideation.similarity import text_similarity, find_duplicate


def test_similarity_identical_is_one_disjoint_is_zero():
    assert text_similarity('robot diffusion','robot diffusion',[])==pytest.approx(1)
    assert text_similarity('robot','banana',[])==0


def test_dedup_against_executed_and_history():
    existing={'executed':'adaptive robot diffusion policy','history':'language scaling'}
    assert find_duplicate('robot adaptive diffusion policy',existing,threshold=.85)=='executed'
    assert find_duplicate('unrelated research',existing,threshold=.85) is None
