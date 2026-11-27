import os
import sys
from pathlib import Path
import pytest
from src.ideation.role_runner import run_role, RoleAgent
from src.ideation.pool import PoolStore, Match
from tests.ideation.test_pool import candidate


@pytest.fixture(autouse=True)
def isolated_role_home(tmp_path, monkeypatch):
    home = tmp_path / 'user-home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('WRAPPER_PYTHON', sys.executable)
    monkeypatch.delenv('CODEX_HOME', raising=False)
    monkeypatch.delenv('AGSCI_ROLE_HOME', raising=False)


@pytest.mark.parametrize('content,expected',[('{"winner":"a"}',{'winner':'a'}),('invalid',None),(None,None)])
def test_role_script_returns_json(tmp_path,monkeypatch,content,expected):
    bin_dir=tmp_path/'bin'; bin_dir.mkdir()
    fake=bin_dir/'headless'
    fake.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\n'+(f'Path("verdict.json").write_text({content!r})\n' if content is not None else 'pass\n'))
    fake.chmod(0o755)
    monkeypatch.setenv('HEADLESS_BIN',str(fake))
    monkeypatch.setenv('PATH',str(bin_dir)+os.pathsep+os.environ['PATH'])
    result=run_role('judge_pairwise','Judge.',out_dir=tmp_path/'role',agent=RoleAgent('codex',None,None),timeout_seconds=10,container=None)
    assert result==expected


def test_invalid_judge_output_no_elo_change(tmp_path):
    def runner(cmd,timeout):
        Path(cmd[cmd.index('--output-dir')+1],'verdict.json').write_text('[]'); return 0
    result=run_role('judge_pairwise','Judge',out_dir=tmp_path/'role',agent=RoleAgent('codex',None,None),timeout_seconds=1,container=None,runner=runner)
    assert result is None
    store=PoolStore(tmp_path/'pool')
    for id in ['a','b']: store.add_candidate(candidate(id))
    store.add_match(Match(1,0,'a','b','pairwise',None,'invalid'))
    assert store.load().elo=={'a':1200,'b':1200}


def test_timeout_returns_none(tmp_path):
    import subprocess
    def runner(cmd,timeout): raise subprocess.TimeoutExpired(cmd,timeout)
    assert run_role('generate','Generate',out_dir=tmp_path,agent=RoleAgent('codex',None,None),timeout_seconds=1,container=None,runner=runner) is None


def test_role_does_not_reuse_campaign_session(tmp_path,monkeypatch):
    monkeypatch.setenv('AGENT_CONTAINER_SESSION_NAME','campaign-session')
    fake=tmp_path/'headless'
    fake.write_text('#!/usr/bin/env python3\nimport json,os\nfrom pathlib import Path\nPath("verdict.json").write_text(json.dumps({"session":os.environ.get("AGENT_CONTAINER_SESSION_NAME","")}))\n')
    fake.chmod(0o755); monkeypatch.setenv('HEADLESS_BIN',str(fake))
    assert run_role('judge_pairwise','Judge',out_dir=tmp_path/'role',agent=RoleAgent('codex'),timeout_seconds=10,container=None)=={'session':''}


def test_role_reply_writes_expected_output_file(tmp_path, monkeypatch):
    fake = tmp_path / 'headless'
    fake.write_text('#!/usr/bin/env python3\nprint(\'--- final message ---\\n{"winner":"b","rationale":"clear"}\')\n')
    fake.chmod(0o755)
    monkeypatch.setenv('HEADLESS_BIN', str(fake))
    output = tmp_path / 'role'
    result = run_role('judge_pairwise', 'Judge.', out_dir=output, agent=RoleAgent('codex'),
                      timeout_seconds=10, container=None)
    assert result == {'winner': 'b', 'rationale': 'clear'}
    assert (output / 'verdict.json').read_text() == '{"winner": "b", "rationale": "clear"}'


def test_role_file_mode_remains_fallback(tmp_path, monkeypatch):
    fake = tmp_path / 'headless'
    fake.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\nPath("verdict.json").write_text(\'{"winner":"a"}\')\n')
    fake.chmod(0o755)
    monkeypatch.setenv('HEADLESS_BIN', str(fake))
    assert run_role('judge_pairwise', 'Judge.', out_dir=tmp_path / 'role', agent=RoleAgent('codex'),
                    timeout_seconds=10, container=None) == {'winner': 'a'}


@pytest.mark.parametrize('agent,opt_out', [('codex', False), ('codex', True), ('antigravity', False)])
def test_role_home_environment_and_cleanup(tmp_path, monkeypatch, agent, opt_out):
    import json
    home = Path(os.environ['HOME'])
    source = tmp_path / 'codex-config'
    source.mkdir()
    (source / 'auth.json').write_text('{"token":"fresh"}')
    (source / 'config.toml').write_text('model = "test"\n[model_providers.proxy]\nbase_url = "http://localhost/v1"\n')
    monkeypatch.setenv('CODEX_HOME', str(source))
    base = tmp_path / 'minimal-homes'
    monkeypatch.setenv('AGSCI_ROLE_HOME', 'off' if opt_out else str(base))
    fake = tmp_path / 'headless'
    fake.write_text('''#!/usr/bin/env python3
import json, os
from pathlib import Path
h = Path.home()
c = Path(os.environ['CODEX_HOME'])
print('--- final message ---')
print(json.dumps({'home':str(h), 'codex_home':str(c), 'auth':json.loads((c/'auth.json').read_text()),
                  'config':(c/'config.toml').read_text()}))
''')
    fake.chmod(0o755)
    monkeypatch.setenv('HEADLESS_BIN', str(fake))
    result = run_role('judge_pairwise', 'Judge', out_dir=tmp_path / 'role',
                      agent=RoleAgent(agent), timeout_seconds=10, container=None)
    assert result is not None
    assert result['auth'] == {'token': 'fresh'}
    if agent == 'codex' and not opt_out:
        assert Path(result['home']).parent == base
        assert result['codex_home'] == str(Path(result['home']) / '.codex')
        assert 'base_url' in result['config']
        assert not Path(result['home']).exists()
    else:
        assert result['home'] == str(home)
        assert result['codex_home'] == str(source)
        assert not base.exists()
    assert json.loads((source / 'auth.json').read_text()) == {'token': 'fresh'}


def test_role_failure_cleans_up_home(tmp_path, monkeypatch):
    fake = tmp_path / 'headless'
    fake.write_text('#!/bin/sh\nexit 7\n')
    fake.chmod(0o755)
    monkeypatch.setenv('HEADLESS_BIN', str(fake))
    base = tmp_path / 'minimal-homes'
    monkeypatch.setenv('AGSCI_ROLE_HOME', str(base))
    assert run_role('judge_pairwise', 'Judge', out_dir=tmp_path / 'role', agent=RoleAgent('codex'),
                    timeout_seconds=10, container=None) is None
    assert list(base.iterdir()) == []


def test_container_role_does_not_prepare_host_home(tmp_path, monkeypatch):
    import subprocess
    script = Path(__file__).resolve().parents[2] / 'src/scripts/ideation_role.sh'
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('Judge')
    fake_docker = tmp_path / 'docker'
    fake_docker.write_text('#!/bin/sh\nexit 71\n')
    fake_docker.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    # Invalid for host preparation; container launches must never use it.
    monkeypatch.setenv('AGSCI_ROLE_HOME', os.environ['HOME'])
    monkeypatch.setenv('AGENT_DOCKER_READY_RETRIES', '1')
    monkeypatch.setenv('AGENT_DOCKER_RUN_RETRIES', '1')
    run = subprocess.run(['bash', str(script), '--agent', 'codex', '--output-dir', str(tmp_path / 'role'),
                          '--prompt-file', str(prompt), '--expected-output', 'verdict.json',
                          '--containerize', '--container-image', 'test:latest'],
                         capture_output=True, text=True, timeout=10)
    assert run.returncode != 0  # Fake Docker intentionally cannot launch anything.
    assert '[role_home]' not in run.stderr
    assert not (Path(os.environ['HOME']) / '.codex').exists()


def test_timeout_cleans_up_minimal_home(tmp_path, monkeypatch):
    fake = tmp_path / 'headless'
    fake.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n')
    fake.chmod(0o755)
    monkeypatch.setenv('HEADLESS_BIN', str(fake))
    monkeypatch.delenv('WRAPPER_PYTHON', raising=False)  # Runner must select its own Python.
    base = tmp_path / 'minimal-homes'
    monkeypatch.setenv('AGSCI_ROLE_HOME', str(base))
    assert run_role('judge_pairwise', 'Judge', out_dir=tmp_path / 'role', agent=RoleAgent('codex'),
                    timeout_seconds=1, container=None) is None
    assert list(base.iterdir()) == []


def test_refreshed_auth_is_retained_with_warning(tmp_path, monkeypatch, capfd):
    home = Path(os.environ['HOME'])
    (home / '.codex').mkdir()
    (home / '.codex/auth.json').write_text('{"token":"original"}')
    fake = tmp_path / 'headless'
    fake.write_text('''#!/usr/bin/env python3
from pathlib import Path
(Path.home()/'.codex/auth.json').write_text('{"token":"refreshed"}')
print('--- final message ---\n{"winner":"a"}')
'''.replace("--- final message ---\n", "--- final message ---\\n"))
    fake.chmod(0o755)
    monkeypatch.setenv('HEADLESS_BIN', str(fake))
    base = tmp_path / 'minimal-homes'
    monkeypatch.setenv('AGSCI_ROLE_HOME', str(base))
    assert run_role('judge_pairwise', 'Judge', out_dir=tmp_path / 'role', agent=RoleAgent('codex'),
                    timeout_seconds=10, container=None) == {'winner': 'a'}
    assert (home / '.codex/auth.json').read_text() == '{"token":"original"}'
    retained = list(base.iterdir())
    assert len(retained) == 1
    assert (retained[0] / '.codex/auth.json').read_text() == '{"token":"refreshed"}'
    assert 'refreshed credentials retained privately' in capfd.readouterr().err
