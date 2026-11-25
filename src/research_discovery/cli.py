"""CLI for standalone research-gap discovery; no ideation wiring."""
import argparse
from pathlib import Path
import sys
from src.ideation.role_runner import RoleAgent
from .config import DiscoverySettings
from .driver import run_discovery


def build_parser():
    parser=argparse.ArgumentParser(prog='agsci discover')
    commands=parser.add_subparsers(dest='command',required=True)
    run=commands.add_parser('run')
    run.add_argument('--topic',required=True)
    run.add_argument('--papers',action='append',required=True,help='Folder, BibTeX, identifier list, arXiv id or DOI; repeatable')
    run.add_argument('--manuscript',type=Path,required=True)
    run.add_argument('--out',type=Path,required=True)
    run.add_argument('--source',action='append',default=[])
    run.add_argument('--agent',default='codex');run.add_argument('--model');run.add_argument('--reasoning-effort')
    run.add_argument('--judge-agent',default='claude');run.add_argument('--judge-model')
    run.add_argument('--offline',action='store_true')
    run.add_argument('--max-claims',type=int,default=160)
    run.add_argument('--context-passages',type=int,default=48)
    run.add_argument('--max-papers',type=int,default=100);run.add_argument('--max-gaps',type=int,default=10)
    run.add_argument('--deep-papers',type=int,default=40);run.add_argument('--parallel',type=int,default=4)
    run.add_argument('--search-limit',type=int,default=8);run.add_argument('--expansion-limit',type=int,default=5)
    return parser


def main(argv=None):
    args=build_parser().parse_args(argv)
    try:
        settings=DiscoverySettings(max_papers=args.max_papers,max_gaps=args.max_gaps,deep_papers=args.deep_papers,
            parallel=args.parallel,search_limit=args.search_limit,
            expansion_limit=args.expansion_limit,offline=args.offline,max_discovery_claims=args.max_claims,context_passages=args.context_passages)
        result=run_discovery(topic=args.topic,papers=args.papers,manuscript=args.manuscript,out_dir=args.out,
            generator=RoleAgent(args.agent,args.model,args.reasoning_effort),
            judge=RoleAgent(args.judge_agent,args.judge_model,args.reasoning_effort),settings=settings,sources=args.source)
        print(result);return 0
    except (ValueError,OSError) as exc:
        print('agsci discover: '+str(exc),file=sys.stderr);return 2
