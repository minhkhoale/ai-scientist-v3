import pytest

from src.ideation.role_reply import parse_final_reply


@pytest.mark.parametrize(
    'reply,expected',
    [
        ('{"winner":"a"}', {'winner': 'a'}),
        ('```json\n{"winner":"a"}\n```', {'winner': 'a'}),
    ],
)
def test_parse_final_reply_accepts_object_or_json_fence(reply, expected):
    assert parse_final_reply(f'log output\n--- final message ---\n{reply}\n') == expected


@pytest.mark.parametrize('reply', ['Answer: {"winner":"a"}', '{"winner":"a"}\nDone', '[1, 2]'])
def test_parse_final_reply_rejects_prose_and_non_object(reply):
    with pytest.raises(ValueError):
        parse_final_reply(f'--- final message ---\n{reply}')
