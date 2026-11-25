"""Proposal ranking and explicit human selection."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

from src.ideation.literature.corpus import build_corpus, load_corpus, _atomic_write
from src.ideation.role_runner import RoleAgent
from src.ideation.round import RoundSettings, _rows
from .driver import ProposalSettings, run_proposal


def build_parser():
    parser = argparse.ArgumentParser(prog='agsci propose')
    subs = parser.add_subparsers(dest='command', required=True)
    run = subs.add_parser('run', help='Rank research proposals without experiments')
    goal = run.add_mutually_exclusive_group(required=True)
    goal.add_argument('--goal')
    goal.add_argument('--goal-file', type=Path)
    run.add_argument('--codebase', type=Path)
    run.add_argument('--source', action='append', required=True)
    run.add_argument('--agent', default='codex')
    run.add_argument('--model')
    run.add_argument('--reasoning-effort')
    run.add_argument('--judge-agent')
    run.add_argument('--judge-model')
    run.add_argument('--rounds', type=int, default=4)
    run.add_argument('--per-round', type=int, default=3)
    run.add_argument('--matches-per-round', type=int, default=6)
    run.add_argument('--out', type=Path)
    select = subs.add_parser('select', help='Choose proposals to execute in the given order')
    select.add_argument('run_dir', type=Path)
    select.add_argument('candidate_ids', nargs='+')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'select':
            rows = {r['id']:r for r in _rows(args.run_dir/'proposals.jsonl')}
            unknown = set(args.candidate_ids)-rows.keys()
            if unknown: raise ValueError('unknown candidate ids: '+', '.join(sorted(unknown)))
            if len(set(args.candidate_ids)) != len(args.candidate_ids): raise ValueError('duplicate candidate ids')
            data = ''.join(json.dumps(rows[id])+'\n' for id in args.candidate_ids)
            _atomic_write(args.run_dir/'selected.jsonl',data.encode())
            print(args.run_dir/'selected.jsonl')
            return 0
        goal = args.goal if args.goal is not None else args.goal_file.read_text()
        if not goal.strip(): raise ValueError('goal must not be empty')
        if args.rounds < 1 or args.per_round < 1 or args.matches_per_round < 0:
            raise ValueError('rounds and per-round must be positive; matches-per-round must be nonnegative')
        if args.codebase is not None and not args.codebase.is_dir(): raise ValueError('codebase must be a directory')
        slug = '-'.join(re.findall(r'[a-z0-9]+',goal.lower())[:8]) or 'research'
        out = args.out or Path('results/proposals')/slug/datetime.now().strftime('%Y%m%d_%H%M%S')
        corpus_dir = out/'literature'
        if not corpus_dir.exists():
            build_corpus(corpus_dir,args.source,require_nonempty=True)
        corpus = load_corpus(corpus_dir)
        generator = RoleAgent(args.agent,args.model,args.reasoning_effort)
        judge = RoleAgent(args.judge_agent or args.agent,args.judge_model or args.model,args.reasoning_effort)
        path = run_proposal(goal=goal,out_dir=out,corpus=corpus,codebase=args.codebase,
            generator=generator,judge=judge,settings=ProposalSettings(rounds=args.rounds,
                round=RoundSettings(per_round=args.per_round,matches_per_round=args.matches_per_round,metareview_every=1)))
        print(path)
        print('ID\tElo\tTitle')
        for row in _rows(out/'proposals.jsonl')[:5]:
            print(f"{row['id']}\t{row['elo']:.1f}\t{row['bundle']['title']}")
        return 0
    except (ValueError,OSError) as exc:
        print(f'agsci propose: {exc}',file=sys.stderr)
        return 2
