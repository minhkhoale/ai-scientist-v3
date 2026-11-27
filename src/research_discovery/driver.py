"""Resumable entry point for research-graph gap discovery."""
from dataclasses import asdict
from pathlib import Path

from src.ideation.role_runner import RoleAgent
from .artifacts import digest, fingerprint_inputs, native_role, read_json, write_json
from .config import DiscoverySettings
from .graph_pipeline import run_graph_discovery
from .models import ValidationError
from .providers import ScholarProvider


def run_discovery(*, topic, papers, manuscript, out_dir, generator=RoleAgent('codex'),
                  judge=RoleAgent('claude'), settings=DiscoverySettings(), provider=None,
                  run_role_fn=native_role, sources=(), log=print):
    if not isinstance(topic, str) or not topic.strip():
        raise ValidationError('topic is required')
    manuscript = Path(manuscript).expanduser().resolve()
    out = Path(out_dir).expanduser().resolve()
    if not manuscript.is_file():
        raise ValidationError('manuscript does not exist')
    for item in papers:
        path = Path(item).expanduser()
        if path.is_dir() and out.is_relative_to(path.resolve()):
            raise ValidationError('output must be outside seed corpus directories')
    if out == manuscript:
        raise ValidationError('output cannot replace manuscript')
    inputs = dict(topic=topic, inputs=fingerprint_inputs(papers, manuscript), settings=asdict(settings),
                  generator=asdict(generator), judge=asdict(judge), sources=list(sources))
    key = digest(inputs)
    saved = out / 'inputs.json'
    if saved.exists():
        if read_json(saved)['sha256'] != key:
            raise ValidationError('run inputs differ; choose a new output directory')
    elif out.exists() and any(out.iterdir()):
        raise ValidationError('output is nonempty and is not a discovery run')
    out.mkdir(parents=True, exist_ok=True)
    write_json(saved, {'sha256': key, 'inputs': inputs})
    status = out / 'status.json'
    if status.exists() and read_json(status).get('state') in ('completed', 'no_supported_gaps'):
        if (out / 'report.md').exists():
            return out / 'report.md'
    write_json(status, {'state': 'running', 'stage': 'retrieval'})
    try:
        return run_graph_discovery(topic=topic, papers=papers, manuscript=manuscript, out=out,
            provider=provider or ScholarProvider(), settings=settings, generator=generator, judge=judge,
            run_role_fn=run_role_fn, sources=sources, log=log)
    except Exception as exc:
        write_json(status, {'state': 'failed', 'error': str(exc)})
        raise
