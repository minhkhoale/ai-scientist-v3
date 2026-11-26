"""Standalone defaults; existing pipeline configurations are unchanged."""
from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoverySettings:
    search_limit: int = 8
    expansion_limit: int = 5
    max_papers: int = 100
    max_gaps: int = 10
    passage_chars: int = 3500
    extraction_batch: int = 24  # manuscript passages per extraction call
    max_discovery_claims: int = 160
    context_passages: int = 48
    role_timeout: int = 1800
    deep_papers: int = 40
    parallel: int = 4
    deep_batch: int = 2
    abstract_batch: int = 10
    max_targeted_papers: int = 10
    offline: bool = False

    def __post_init__(self):
        for key in ('search_limit','expansion_limit','max_papers','max_gaps','passage_chars','extraction_batch','max_discovery_claims','context_passages','role_timeout','deep_papers','parallel','deep_batch','abstract_batch','max_targeted_papers'):
            value = getattr(self,key)
            if type(value) is not int or value < (0 if key in ('search_limit','expansion_limit') else 1):
                raise ValueError('invalid '+key)
        if type(self.offline) is not bool:
            raise ValueError('offline must be boolean')
