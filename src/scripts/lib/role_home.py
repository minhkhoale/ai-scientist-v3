"""Private per-call Codex homes without interactive skills, plugins or hooks.

prepare USER_HOME BASE [CODEX_HOME] prints a fresh home. finish USER_HOME ROLE_HOME
removes it, unless file credentials were refreshed (retained privately for recovery).
The user's Codex configuration and credentials are never modified.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

# Preserve connectivity, model selection and execution restrictions, not extensions
# or user instructions. Opt out in the shell to retain other custom settings.
CONFIG_KEYS = frozenset('''
model model_provider model_providers model_reasoning_effort model_reasoning_summary
model_verbosity model_context_window model_auto_compact_token_limit model_catalog_json
base_url openai_base_url preferred_auth_method forced_login_method forced_chatgpt_workspace_id
cli_auth_credentials_store chatgpt_base_url http network sandbox_mode approval_policy
sandbox_workspace_write shell_environment_policy disable_response_storage
'''.split())
STATE_FILE = '.agsci-role-state.json'


def _toml_value(value):
    if isinstance(value, dict):
        return '{' + ', '.join(f'{json.dumps(k)} = {_toml_value(v)}' for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(_toml_value(v) for v in value) + ']'
    if isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=False)
    raise ValueError('unsupported value in Codex role configuration')


def role_config(user_config: str) -> str:
    config = tomllib.loads(user_config)
    profile = config.get('profile')
    if profile:
        config.update(config.get('profiles', {})[profile])
    selected = {k: v for k, v in config.items() if k in CONFIG_KEYS}
    selected['features'] = {'hooks': False}
    return ''.join(f'{k} = {_toml_value(v)}\n' for k, v in selected.items())


def _write_private(target: Path, data: bytes) -> None:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _read_auth(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def prepare(user_home: Path, base: Path, codex_home: Path | None = None) -> Path:
    user_home, base = user_home.resolve(), base.resolve()
    source = (codex_home or user_home / '.codex').resolve()
    if base == user_home or base == source or source in base.parents:
        raise ValueError('role home base must be separate from the user and Codex homes')
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if base.stat().st_uid != os.getuid():
        raise ValueError('role home base must belong to the current user')
    base.chmod(0o700)
    home = Path(tempfile.mkdtemp(dir=base, prefix='role-'))
    try:
        target = home / '.codex'
        target.mkdir(mode=0o700)
        config_file = source / 'config.toml'
        config = role_config(config_file.read_text() if config_file.exists() else '')
        # Relative catalogs still refer to the original configuration directory.
        parsed = tomllib.loads(config)
        if 'model_catalog_json' in parsed:
            parsed['model_catalog_json'] = str(source / parsed['model_catalog_json'])
            config = ''.join(f'{k} = {_toml_value(v)}\n' for k, v in parsed.items())
        _write_private(target / 'config.toml', config.encode())
        auth = _read_auth(source / 'auth.json')
        if auth is not None:
            _write_private(target / 'auth.json', auth)
        _write_private(home / STATE_FILE, json.dumps({
            'user_home': str(user_home), 'auth_digest': _digest(auth),
        }).encode())
        if (user_home / '.npm').is_dir():
            (home / '.npm').symlink_to(user_home / '.npm')
        return home
    except BaseException:
        shutil.rmtree(home)
        raise


def finish(user_home: Path, role_home: Path) -> None:
    if role_home.resolve() == user_home.resolve() or not role_home.name.startswith('role-'):
        raise ValueError('not a generated role home')
    state = json.loads((role_home / STATE_FILE).read_text())
    if state['user_home'] != str(user_home.resolve()):
        raise ValueError('role home belongs to a different user home')
    refreshed = _read_auth(role_home / '.codex/auth.json')
    if refreshed is not None and _digest(refreshed) != state['auth_digest']:
        print(f'[role_home] refreshed credentials retained privately in {role_home}; user credentials unchanged', file=sys.stderr)
        return
    shutil.rmtree(role_home)


if __name__ == '__main__':
    command, user_home, location = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    try:
        if command == 'prepare':
            print(prepare(user_home, location, Path(sys.argv[4]) if len(sys.argv) > 4 else None))
        elif command == 'finish':
            finish(user_home, location)
        else:
            raise ValueError('unknown role home command')
    except (OSError, ValueError, KeyError) as error:
        # Never echo configuration values or credential contents on errors.
        print(f'[role_home] {command} failed ({type(error).__name__}); check paths/config or set AGSCI_ROLE_HOME=off', file=sys.stderr)
        raise SystemExit(1)
