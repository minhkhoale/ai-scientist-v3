"""Minimal role homes preserve connectivity, not interactive extensions."""
import concurrent.futures
import json
import stat
import tomllib

import pytest

from src.scripts.lib.role_home import finish, prepare, role_config


@pytest.fixture
def user_home(tmp_path):
    home = tmp_path / 'user'
    (home / '.codex').mkdir(parents=True)
    (home / '.codex/auth.json').write_text('{"token":"original"}')
    return home


def test_config_preserves_provider_and_security_but_not_extensions():
    config = tomllib.loads(role_config('''model = "test-model"
model_provider = "local"
sandbox_mode = "read-only"
[model_providers.local]
name = "Local"
base_url = "http://localhost:10100/v1"
http_headers = { "X-Test" = "yes" }
[http]
proxy = "http://localhost:8888"
[shell_environment_policy]
inherit = "none"
[features]
hooks = true
[plugins.example]
enabled = true
[mcp_servers.example]
command = "unused"
[skills]
config = [{ path = "/unused" }]
'''))
    assert config['model_providers']['local']['base_url'] == 'http://localhost:10100/v1'
    assert config['model_providers']['local']['http_headers'] == {'X-Test': 'yes'}
    assert config['http']['proxy'] == 'http://localhost:8888'
    assert config['shell_environment_policy']['inherit'] == 'none'
    assert config['sandbox_mode'] == 'read-only'
    assert config['features']['hooks'] is False
    assert not {'plugins', 'mcp_servers', 'skills'} & config.keys()


def test_selected_profile_keeps_model_and_provider():
    config = tomllib.loads(role_config('''profile = "local"
[profiles.local]
model = "profile-model"
model_provider = "proxy"
[model_providers.proxy]
base_url = "http://localhost:10100/v1"
'''))
    assert config['model'] == 'profile-model'
    assert config['model_provider'] == 'proxy'
    assert 'profile' not in config


def test_prepare_is_private_and_refreshes_auth_without_loading_old_extensions(user_home, tmp_path):
    base = tmp_path / 'roles'
    (base / '.codex/skills').mkdir(parents=True)
    (user_home / '.npm').mkdir()
    first = prepare(user_home, base)
    (user_home / '.codex/auth.json').write_text('{"token":"newer"}')
    second = prepare(user_home, base)
    assert first != second
    assert (first / '.codex/auth.json').read_text() == '{"token":"original"}'
    assert (second / '.codex/auth.json').read_text() == '{"token":"newer"}'
    assert not (first / '.codex/skills').exists()
    assert (first / '.npm').resolve() == user_home / '.npm'
    for path in (first, first / '.codex', first / '.codex/auth.json', first / '.codex/config.toml'):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def test_prepare_rejects_user_home_alias(user_home, tmp_path):
    alias = tmp_path / 'alias'
    alias.symlink_to(user_home, target_is_directory=True)
    with pytest.raises(ValueError):
        prepare(user_home, alias)
    assert (user_home / '.codex/auth.json').read_text() == '{"token":"original"}'


def test_finish_never_overwrites_new_user_auth(user_home, tmp_path):
    role = prepare(user_home, tmp_path / 'roles')
    (user_home / '.codex/auth.json').write_text('{"token":"interactive-refresh"}')
    (role / '.codex/auth.json').write_text('{"token":"stale-role-refresh"}')
    finish(user_home, role)
    assert json.loads((user_home / '.codex/auth.json').read_text())['token'] == 'interactive-refresh'


def test_finish_retains_refresh_without_writing_user_auth(user_home, tmp_path):
    role = prepare(user_home, tmp_path / 'roles')
    (role / '.codex/auth.json').write_text('{"token":"role-refresh"}')
    finish(user_home, role)
    assert json.loads((user_home / '.codex/auth.json').read_text())['token'] == 'original'
    assert (role / '.codex/auth.json').exists()
    assert stat.S_IMODE((role / '.codex/auth.json').stat().st_mode) == 0o600


def test_parallel_roles_do_not_share_home_or_clobber_first_refresh(user_home, tmp_path):
    base = tmp_path / 'roles'
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        homes = list(pool.map(lambda _: prepare(user_home, base), range(4)))
    assert len(set(homes)) == 4
    for i, home in enumerate(homes):
        (home / '.codex/auth.json').write_text(json.dumps({'token': f'refresh-{i}'}))
    finish(user_home, homes[0])
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda home: finish(user_home, home), homes[1:]))
    assert json.loads((user_home / '.codex/auth.json').read_text())['token'] == 'original'


def test_missing_auth_does_not_reuse_stale_credentials(user_home, tmp_path):
    base = tmp_path / 'roles'
    first = prepare(user_home, base)
    (user_home / '.codex/auth.json').unlink()
    second = prepare(user_home, base)
    assert (first / '.codex/auth.json').exists()
    assert not (second / '.codex/auth.json').exists()


def test_finish_removes_unchanged_home(user_home, tmp_path):
    home = prepare(user_home, tmp_path / 'roles')
    finish(user_home, home)
    assert not home.exists()
    assert (user_home / '.codex/auth.json').exists()


def test_prepare_respects_explicit_codex_home(user_home, tmp_path):
    source = tmp_path / 'custom-codex'
    source.mkdir()
    (source / 'config.toml').write_text('model = "custom"\nmodel_catalog_json = "catalog.json"\n')
    (source / 'auth.json').write_text('{"token":"custom"}')
    home = prepare(user_home, tmp_path / 'roles', source)
    config = tomllib.loads((home / '.codex/config.toml').read_text())
    assert config['model'] == 'custom'
    assert config['model_catalog_json'] == str(source / 'catalog.json')
    assert json.loads((home / '.codex/auth.json').read_text())['token'] == 'custom'


def test_invalid_config_leaves_no_copied_auth(user_home, tmp_path):
    (user_home / '.codex/config.toml').write_text('model = invalid')
    base = tmp_path / 'roles'
    with pytest.raises(ValueError):
        prepare(user_home, base)
    assert list(base.iterdir()) == []
