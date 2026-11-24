import os
from pathlib import Path
import pytest
from src.ideation.role_runner import run_role, RoleAgent
from src.ideation.pool import PoolStore, Match
from tests.ideation.test_pool import candidate


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
