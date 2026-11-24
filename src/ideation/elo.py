"""Elo matchmaking and calibration against measured score deltas."""
from __future__ import annotations
from itertools import combinations
import math
import random
from typing import Callable, Literal, Sequence, TYPE_CHECKING
if TYPE_CHECKING:
    from .pool import Candidate, PoolState, Outcome


def expected_score(a: float, b: float) -> float:
    return 1/(1+10**((b-a)/400))


def update(elo_a: float, elo_b: float, winner: Literal['a','b'], min_played: int) -> tuple[float,float]:
    delta = (32 if min_played<5 else 16)*((1 if winner=='a' else 0)-expected_score(elo_a,elo_b))
    return elo_a+delta,elo_b-delta


def select_pair(eligible: Sequence[Candidate], state: PoolState, rng: random.Random,
                similarity: Callable[[str,str],float]) -> tuple[str,str] | None:
    ids = [c.id for c in eligible]
    if len(ids)<2: return None
    new = [id for id in ids if state.matches_played[id]<3]
    warm = [id for id in ids if state.matches_played[id]>=3]
    r = rng.random()
    if r<.4 and new and warm:
        a = rng.choice(new); return a,min(warm,key=lambda b:(abs(state.elo[a]-state.elo[b]),b))
    if r<.8 and len(warm)>=2:
        pairs = [p for p in combinations(warm,2) if abs(state.elo[p[0]]-state.elo[p[1]])<=200][:20]
        if pairs:
            weights = [math.exp(-abs(state.elo[a]-state.elo[b])/200)*max(1-similarity(a,b),.05) for a,b in pairs]
            return rng.choices(pairs,weights=weights,k=1)[0]
    top = sorted(ids,key=lambda id:(-state.elo[id],id))[:max(2,(len(ids)+1)//2)]
    a, b = rng.sample(top,2)
    return a, b


def select_mode(state: PoolState, a: str, b: str) -> Literal['pairwise','debate']:
    return 'debate' if min(state.matches_played[a],state.matches_played[b])<2 or abs(state.elo[a]-state.elo[b])<50 else 'pairwise'


def reality_calibration(outcomes: Sequence[Outcome]) -> dict[str,float | int | None]:
    rows = [(o.elo_at_selection, o.combined_score-o.baseline_score)
            for o in outcomes if o.combined_score is not None and o.baseline_score is not None
            and all(math.isfinite(v) for v in (o.combined_score,o.baseline_score,o.elo_at_selection))]
    n = len(rows)
    if n<3: return {'n':n,'spearman':None}
    def ranks(values):
        return [1+sum(x<v for x in values)+(sum(x==v for x in values)-1)/2 for v in values]
    a = ranks([elo for elo, _ in rows]); b = ranks([delta for _, delta in rows])
    mean = (n+1)/2
    denominator = math.sqrt(sum((x-mean)**2 for x in a)*sum((x-mean)**2 for x in b))
    return {'n':n,'spearman':sum((x-mean)*(y-mean) for x,y in zip(a,b))/denominator if denominator else None}
