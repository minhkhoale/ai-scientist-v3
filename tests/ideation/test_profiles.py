from src.ideation.profiles import PROFILES
from src.ideation.round import run_tournament_round
from tests.ideation.test_round import fake_roles, round_args


def test_execution_profile_preserves_pool_rows(tmp_path):
    stores = []
    for explicit in (False, True):
        role, _ = fake_roles()
        args = round_args(tmp_path / str(explicit), role)
        if explicit:
            args['profile'] = PROFILES['execution']
        run_tournament_round(**args)
        stores.append({name: args['store']._rows(name) for name in ('candidates','reviews','matches','feedback')})
    assert stores[0] == stores[1]
