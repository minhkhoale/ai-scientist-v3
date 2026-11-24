"""Single-writer, append-only candidate events and deterministic Elo replay."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import json
from pathlib import Path
from typing import Any, Literal
from src.orchestrator.state import append_jsonl

CandidateStatus = Literal['draft','rejected','eligible','executed','invalid','retired']


@dataclass
class Candidate:
    id: str
    cycle: int
    strategy: str
    parent_ids: tuple[str,...]
    bundle: dict[str,Any]
    citations: tuple[str,...]
    rationale: str


@dataclass
class Review:
    candidate_id: str
    verdict: Literal['pass','neutral','already_explained','already_tried','not_implementable','disproved']
    summary: str
    citations: tuple[str,...]
    rejected_citations: int


@dataclass
class Match:
    cycle: int
    match_index: int
    a: str
    b: str
    mode: Literal['pairwise','debate']
    winner: Literal['a','b'] | None
    rationale: str


@dataclass
class Outcome:
    candidate_id: str
    cycle: int
    attempt_id: str
    elo_at_selection: float
    combined_score: float | None
    baseline_score: float | None
    attempt_pass: bool
    falsifier_outcome: str


@dataclass
class PoolState:
    candidates: dict[str,Candidate] = field(default_factory=dict)
    status: dict[str,CandidateStatus] = field(default_factory=dict)
    elo: dict[str,float] = field(default_factory=dict)
    matches_played: dict[str,int] = field(default_factory=dict)
    reviews: dict[str,Review] = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)
    feedback: list[dict] = field(default_factory=list)

    def eligible(self) -> list[Candidate]:
        return sorted((c for id,c in self.candidates.items() if self.status[id]=='eligible'),key=lambda c:(-self.elo[c.id],c.id))


class PoolStore:
    def __init__(self, root: Path):
        self.root = root

    def _rows(self, name: str) -> list[dict]:
        path = self.root/(name+'.jsonl')
        if not path.exists(): return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _append(self, name: str, row: dict):
        self.root.mkdir(parents=True,exist_ok=True)
        append_jsonl(self.root/(name+'.jsonl'),row)

    def add_candidate(self, candidate: Candidate):
        if candidate.id not in self.load().candidates:
            self._append('candidates',{'event':'candidate',**asdict(candidate)})

    def mark(self, candidate_id: str, status: CandidateStatus, reason: str):
        if self.load().status.get(candidate_id)!=status:
            self._append('candidates',{'event':'status','candidate_id':candidate_id,'status':status,'reason':reason})

    def add_review(self, review: Review):
        if review.candidate_id not in self.load().reviews: self._append('reviews',asdict(review))

    def add_match(self, match: Match):
        if not any((r['cycle'],r['match_index'])==(match.cycle,match.match_index) for r in self._rows('matches')):
            self._append('matches',asdict(match))

    def add_outcome(self, outcome: Outcome):
        if not any(r['attempt_id']==outcome.attempt_id for r in self._rows('outcomes')):
            self._append('outcomes',asdict(outcome))

    def add_feedback(self, feedback: dict):
        self._append('feedback',feedback)

    def load(self) -> PoolState:
        from .elo import update
        state = PoolState()
        for row in self._rows('candidates'):
            if row.pop('event')=='status':
                state.status[row['candidate_id']] = row['status']; continue
            row['parent_ids']=tuple(row['parent_ids']); row['citations']=tuple(row['citations'])
            candidate = Candidate(**row)
            state.candidates[candidate.id]=candidate; state.status[candidate.id]='draft'
            state.elo[candidate.id]=1200.0; state.matches_played[candidate.id]=0
        for row in self._rows('reviews'):
            row['citations']=tuple(row['citations']); review=Review(**row); state.reviews[review.candidate_id]=review
        seen = set()
        for row in self._rows('matches'):
            key = row['cycle'],row['match_index']
            if key in seen: continue
            seen.add(key)
            a,b = row['a'],row['b']
            if row['winner'] not in ('a','b') or a not in state.elo or b not in state.elo: continue
            state.elo[a],state.elo[b] = update(state.elo[a],state.elo[b],row['winner'],min(state.matches_played[a],state.matches_played[b]))
            state.matches_played[a]+=1; state.matches_played[b]+=1
        state.outcomes=[Outcome(**row) for row in self._rows('outcomes')]
        state.feedback=self._rows('feedback')
        return state
