"""Resumable, native, literature-only proposal tournaments."""
from dataclasses import asdict, dataclass, replace
from functools import partial
import json
from pathlib import Path
import re
from typing import Sequence

from src.ideation.literature.sources import LiteratureRecord
from src.ideation.literature.corpus import _atomic_write
from src.ideation.literature.retrieval import render_pack, pack_ids
from src.ideation.pool import PoolStore
from src.ideation.profiles import PROFILES
from src.ideation.roles import RoleContext, RoleOutputError, OUTPUT_FILE, render_role
from src.ideation.role_runner import RoleAgent, run_role
from src.ideation.round import RoundSettings, run_tournament_round, _read, _write_once, _rows
from .schema import parse_plan, parse_codebase


@dataclass(frozen=True)
class ProposalSettings:
    rounds: int = 4
    stable_rounds: int = 2
    top_k: int = 5
    round: RoundSettings = RoundSettings(metareview_every=1)


def render_overview(raw, *, candidate_ids, citation_ids) -> str:
    if not isinstance(raw, dict) or not isinstance(raw.get('markdown'), str):
        raise RoleOutputError('overview needs markdown text')
    allowed = set(candidate_ids) | set(citation_ids)
    return re.sub(r'\[((?:cand_|L)[^\]\s]*)\]',
                  lambda m: m[0] if m[1] in allowed else '', raw['markdown'])


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (json.dumps(value, indent=2)+'\n').encode())


def run_proposal(*, goal: str, out_dir: Path, corpus: Sequence[LiteratureRecord],
                 codebase: Path | None, generator: RoleAgent, judge: RoleAgent,
                 settings: ProposalSettings, run_role_fn=run_role, log=print) -> Path:
    if min(settings.rounds, settings.stable_rounds, settings.top_k) < 1:
        raise ValueError('rounds, stable_rounds and top_k must be positive')
    out_dir = Path(out_dir)
    codebase = codebase.resolve() if codebase is not None else None
    if codebase is not None and not codebase.is_dir(): raise ValueError('codebase must be a directory')
    out_dir.mkdir(parents=True, exist_ok=True)
    # Reject changed inputs instead of silently mixing two resumable runs.
    inputs = {'goal':goal, 'codebase':str(codebase) if codebase else None,
              'settings':json.loads(json.dumps(asdict(settings))),
              'generator':asdict(generator), 'judge':asdict(judge),
              'corpus_ids':sorted(pack_ids(corpus))}
    prior = _read(out_dir/'inputs.json')
    if prior is not None and prior != inputs: raise ValueError('resume inputs differ from saved run')
    _write_once(out_dir/'inputs.json', inputs)
    if not (out_dir/'goal.md').exists(): (out_dir/'goal.md').write_text(goal+'\n')
    ctx = RoleContext(goal,'','','','',render_pack(corpus),pack_ids(corpus),'',
                      '(proposal mode: no experiments run)', codebase=str(codebase) if codebase else '(no codebase)')
    profile = PROFILES['proposal']

    def role(name, context, **slots):
        directory = out_dir/'steps'/name
        completed = directory/'completed.json'
        if completed.exists(): return _read(completed)
        output = directory/OUTPUT_FILE[name]
        raw = _read(output) if output.exists() else run_role_fn(name,
            render_role(name,context,prompt_dir=profile.prompt_dir,**slots),
            out_dir=directory,agent=generator,timeout_seconds=settings.round.role_timeout_seconds,container=None)
        _write_once(completed,raw)
        return raw

    plan_path = out_dir/'plan.json'
    plan = parse_plan(_read(plan_path) if plan_path.exists() else role('parse_goal',ctx))
    _write_once(plan_path,plan)
    scan = None
    if codebase is not None:
        scan_path = out_dir/'codebase.json'
        scan = parse_codebase(_read(scan_path) if scan_path.exists() else role('codebase_scan',ctx),codebase)
        _write_once(scan_path,scan)
    plan_text = json.dumps(plan,indent=2)
    ctx = replace(ctx,task_goal=plan_text,goal_plan=plan_text,focus_axes=tuple(plan['focus_axes']),
                  codebase=json.dumps({'root':str(codebase), **scan}) if scan is not None else '(no codebase)')
    profile = replace(profile,parse_candidate=partial(profile.parse_candidate,codebase_root=codebase))
    store = PoolStore(out_dir/'ideation')
    snapshots = _rows(store.root/'elo_snapshots.jsonl')
    status = _read(out_dir/'status.json')
    rounds_run = len(snapshots)
    state_name = status['state'] if status else 'completed'
    if status is None:
        for cycle in range(1,settings.rounds+1):
            if cycle <= len(snapshots):
                saved = snapshots[cycle-1]['top_k_ids']
                recent = snapshots[max(0,cycle-settings.stable_rounds):cycle]
                if len(saved) < 2 and len(store.load().eligible()) < 2:
                    state_name = 'no_candidates'
                    rounds_run = cycle
                    break
                if len(recent) == settings.stable_rounds and all(set(s['top_k_ids']) == set(saved) for s in recent):
                    state_name = 'stable'
                    rounds_run = cycle
                    break
                continue
            run_tournament_round(cycle=cycle,store=store,corpus=corpus,ctx_base=ctx,
                settings=settings.round,generator=generator,judge=judge,run_role_fn=run_role_fn,
                work_dir=store.root,history_titles={},log_event_fn=lambda *a,**k:None,profile=profile)
            state = store.load()
            top = [c.id for c in state.eligible()[:settings.top_k]]
            snapshot = {'round':cycle,'top_k_ids':top,'elo':state.elo}
            store._append('elo_snapshots',snapshot)
            snapshots.append(snapshot)
            rounds_run = cycle
            log(f'Proposal round {cycle}: {len(state.eligible())} eligible')
            if len(state.eligible()) < 2:
                state_name = 'no_candidates'
                break
            recent = snapshots[-settings.stable_rounds:]
            if len(recent) == settings.stable_rounds and all(set(s['top_k_ids']) == set(top) for s in recent):
                state_name = 'stable'
                break
        # Persist the stopping decision before the overview role can be interrupted.
        _write(out_dir/'status.json',{'state':state_name,'rounds_run':rounds_run,
                                     'top_k':snapshots[-1]['top_k_ids'] if snapshots else []})
    state = store.load()
    rows = [dict(id=c.id,elo=state.elo[c.id],matches_played=state.matches_played[c.id],bundle=c.bundle,
                 citations=list(c.citations),rationale=c.rationale,
                 review=asdict(state.reviews[c.id]) if c.id in state.reviews else None,
                 verification=asdict(state.verifications[c.id]) if c.id in state.verifications else None)
            for c in state.eligible()]
    _atomic_write(out_dir/'proposals.jsonl', ''.join(json.dumps(r)+'\n' for r in rows).encode())
    raw = role('overview',ctx,OUTCOMES=json.dumps(rows[:settings.top_k]))
    overview = render_overview(raw,candidate_ids=state.candidates,citation_ids=pack_ids(corpus))
    path = out_dir/'overview.md'
    _atomic_write(path,(overview+'\n').encode())
    return path
