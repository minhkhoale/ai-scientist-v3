"""Atomic artifacts and validated, input-keyed, interruption-safe role steps."""
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
from src.ideation.literature.corpus import _atomic_write
from src.ideation.role_runner import run_role, RoleAgent
from .models import ValidationError

LOGGER = logging.getLogger(__name__)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    _atomic_write(path,(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())


def read_json(path):
    return json.loads(path.read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def native_role(name, prompt, **kwargs):
    # Reuse the existing isolated runner/launcher without mutating its role registry.
    return run_role('generate',prompt,**kwargs)


class Steps:
    def __init__(self, root: Path, agent: RoleAgent, run_role_fn=native_role):
        self.root,self.agent,self.run_role_fn = root,agent,run_role_fn

    def run(self, name, step, prompt, validate, *, agent=None):
        selected = agent or self.agent
        directory = self.root/step
        key = digest({'name':name,'prompt':prompt,'agent':asdict(selected)})
        completed = directory/'completed.json'
        if completed.exists():
            saved = read_json(completed)
            if saved['input_sha256'] != key:
                raise ValidationError('completed step inputs differ: '+step)
            return validate(saved['output'])
        directory.mkdir(parents=True,exist_ok=True)
        request=directory/'request.json'
        if request.exists() and read_json(request)['input_sha256']!=key:
            raise ValidationError('unfinished step inputs differ: '+step)
        write_json(request,{'input_sha256':key})
        output = directory/'candidate.json'
        raw=None
        if output.exists():
            try:raw=read_json(output)
            except ValueError:
                output.rename(directory/'partial_output.txt')
            if raw is not None and not isinstance(raw,dict):
                write_json(directory/'invalid_output.json',raw);output.unlink();raw=None
        if raw is None:
            raw=self.run_role_fn(name,prompt,out_dir=directory,agent=selected,
                timeout_seconds=getattr(self,'timeout',1800),container=None)
        if not isinstance(raw,dict):
            raise ValidationError('role failed or did not return an object: '+name)
        try:
            result = validate(raw)
        except ValidationError:
            if output.exists():
                write_json(directory/'invalid_output.json',raw)
                output.unlink()
            raise
        write_json(completed,{'input_sha256':key,'output':raw})
        LOGGER.info('research_discovery_step_completed',extra={'step':step,'role':name})
        return result


def fingerprint_inputs(papers, manuscript):
    result = []
    for supplied in [*papers,manuscript]:
        path = Path(supplied).expanduser()
        if path.is_dir():
            files = sorted(p for p in path.rglob('*') if p.is_file() and not p.is_symlink())
        elif path.is_file():files = [path]
        else:
            result.append({'identifier':str(supplied)})
            continue
        result.append({'path':str(path.resolve()),'files':[(str(p.relative_to(path) if path.is_dir() else p.name),hashlib.sha256(p.read_bytes()).hexdigest()) for p in files]})
    return result
