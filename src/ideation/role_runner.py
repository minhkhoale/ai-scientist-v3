"""A role gets one isolated workspace and returns one JSON object."""
from __future__ import annotations
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
from typing import Callable
from .roles import RoleName, OUTPUT_FILE


@dataclass(frozen=True)
class RoleAgent:
    agent: str
    model: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class ContainerOptions:
    containerize: bool = False
    container_image: str = ''
    container_network: str = 'none'
    container_auth_root: Path | None = None
    container_env_file: Path | None = None
    container_session_name: str | None = None
    container_mount_root: Path | None = None


def _run(cmd: list[str], timeout: int) -> int:
    with subprocess.Popen(cmd,start_new_session=True) as process:
        try:
            return process.wait(timeout=timeout)
        except BaseException:
            try: os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError: pass
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait()
            raise


def run_role(role: RoleName, prompt: str, *, out_dir: Path, agent: RoleAgent,
             timeout_seconds: int, container: ContainerOptions | None,
             runner: Callable[[list[str],int],int] | None = None) -> dict | None:
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True,exist_ok=True)
    prompt += '\nReply with only the JSON object. Do not use tools.\n'
    (out_dir/'prompt.md').write_text(prompt)
    cmd = ['bash',str(Path(__file__).parents[1]/'scripts'/'ideation_role.sh'),'--agent',agent.agent,
           '--output-dir',str(out_dir),'--prompt-file',str(out_dir/'prompt.md'),'--expected-output',OUTPUT_FILE[role]]
    for option,value in [('--agent-model',agent.model),('--reasoning-effort',agent.reasoning_effort)]:
        if value: cmd.extend([option,value])
    if container and container.containerize:
        cmd.append('--containerize')
        for key in ('container_image','container_network','container_auth_root','container_env_file'):
            value = getattr(container,key)
            if value: cmd.extend(['--'+key.replace('_','-'),str(value)])
    try:
        if (runner or _run)(cmd,timeout_seconds)!=0: return None
        value = json.loads((out_dir/OUTPUT_FILE[role]).read_text())
        return value if isinstance(value,dict) else None
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return None
