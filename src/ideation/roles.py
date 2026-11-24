"""Explicit prompt slots and strict role-output contracts."""
from dataclasses import dataclass, asdict
import hashlib
from pathlib import Path
import re
from typing import Any, Literal, Mapping, get_args
from src.orchestrator.idea_bundle import normalize_idea_bundle, IdeaBundleValidationError
from .pool import Candidate, Review

RoleName = Literal['generate','reflect','judge_pairwise','judge_debate','evolve_combine','evolve_simplify','evolve_out_of_box','evolve_feasibility','metareview']
OUTPUT_FILE: Mapping[RoleName,str] = {r:('candidate.json' if r=='generate' or r.startswith('evolve_') else 'review.json' if r=='reflect' else 'feedback.json' if r=='metareview' else 'verdict.json') for r in get_args(RoleName)}


class RoleOutputError(ValueError):
    pass


@dataclass(frozen=True)
class RoleContext:
    task_goal: str
    task_rubrics: str
    harness_rubric_requirements: str
    recent_attempts: str
    best_manifest: str
    literature_pack: str
    allowed_citations: frozenset[str]
    feedback: str
    reality: str


def render_role(role: RoleName, ctx: RoleContext, **slots: str) -> str:
    if role not in OUTPUT_FILE: raise ValueError(f'unknown role {role}')
    text = (Path(__file__).parents[1]/'prompts'/'ideation'/f'{role}.md').read_text()
    values = {key.upper():str(value) for key,value in asdict(ctx).items()}
    values.update(slots)
    # Substitute once: literature and agent data must never become template code.
    text = re.sub(r'{{([A-Z_]+)}}',lambda m:values.get(m[1],m[0]),text)
    if re.search(r'{{.*?}}',text): raise ValueError('unfilled prompt slot')
    return text


def _object(raw):
    if not isinstance(raw,dict): raise RoleOutputError('output must be an object')
    return raw


def _citations(raw,allowed):
    values = raw.get('citations',[])
    if not isinstance(values,list) or any(not isinstance(v,str) for v in values):
        raise RoleOutputError('citations must be strings')
    return tuple(v for v in values if v in allowed),sum(v not in allowed for v in values)


def parse_candidate(raw: Any, *, cycle: int, strategy: str, allowed: frozenset[str]) -> Candidate:
    _object(raw)
    if raw.get('prediction') is None or raw.get('falsifier') is None:
        raise RoleOutputError('prediction and falsifier are required')
    try:
        bundle = normalize_idea_bundle(raw,cycle_index=cycle)
    except (IdeaBundleValidationError,ValueError,TypeError) as exc:
        raise RoleOutputError(str(exc)) from exc
    id = 'cand_'+hashlib.sha1((bundle['title']+'\n'+bundle['hypothesis']).encode()).hexdigest()[:10]
    bundle['idea_id'] = id
    parents = raw.get('parent_ids',[])
    if not isinstance(parents,list) or any(not isinstance(p,str) for p in parents): raise RoleOutputError('invalid parent_ids')
    return Candidate(id,cycle,strategy,tuple(parents),bundle,_citations(raw,allowed)[0],str(raw.get('rationale','')))


def parse_review(raw, *, candidate_id, allowed) -> Review:
    _object(raw)
    if raw.get('verdict') not in ('pass','neutral','already_explained','already_tried','not_implementable','disproved'):
        raise RoleOutputError('unknown review verdict')
    if not isinstance(raw.get('summary',''),str): raise RoleOutputError('invalid summary')
    citations,rejected = _citations(raw,allowed)
    return Review(candidate_id,raw['verdict'],raw.get('summary',''),citations,rejected)


def parse_verdict(raw) -> Literal['a','b']:
    _object(raw)
    if raw.get('winner') not in ('a','b') or not isinstance(raw.get('rationale'),str): raise RoleOutputError('invalid verdict')
    return raw['winner']


def parse_feedback(raw) -> dict:
    _object(raw)
    for key in ('common_weaknesses','common_strengths','focus_areas'):
        if not isinstance(raw.get(key),list) or any(not isinstance(v,str) for v in raw[key]): raise RoleOutputError(f'invalid {key}')
    if not isinstance(raw.get('narrative'),str): raise RoleOutputError('invalid narrative')
    return {key:raw[key] for key in ('common_weaknesses','common_strengths','focus_areas','narrative')}
