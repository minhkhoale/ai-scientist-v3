"""Candidate and prompt interfaces for tournament modes."""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from .pool import Candidate
from .roles import DEFAULT_PROMPT_DIR, parse_candidate


@dataclass(frozen=True)
class Profile:
    name: str
    prompt_dir: Path
    parse_candidate: Callable[..., Candidate]
    candidate_text: Callable[[Candidate], str]
    generate_strategies: tuple[str, ...]


def execution_text(candidate: Candidate) -> str:
    return candidate.bundle['title']+' '+candidate.bundle['hypothesis']


PROFILES: Mapping[str, Profile] = {
    'execution': Profile('execution', DEFAULT_PROMPT_DIR, parse_candidate, execution_text, ('literature',)),
}

from .proposal.schema import parse_proposal_candidate

PROFILES['proposal'] = Profile('proposal', DEFAULT_PROMPT_DIR.parent/'proposal',
    parse_proposal_candidate, lambda c: c.bundle['title']+' '+c.bundle['statement'], ('literature','debate'))
