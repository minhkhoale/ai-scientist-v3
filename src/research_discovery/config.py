"""Standalone defaults; existing pipeline configurations are unchanged."""
from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoverySettings:
    search_limit: int = 8
    expansion_limit: int = 5
    max_papers: int = 100
    max_gaps: int = 12
    passage_chars: int = 3500
    extraction_batch: int = 8
    role_timeout: int = 1800
    offline: bool = False

    def __post_init__(self):
        for key in ('search_limit','expansion_limit','max_papers','max_gaps','passage_chars','extraction_batch','role_timeout'):
            value = getattr(self,key)
            if type(value) is not int or value < (0 if key in ('search_limit','expansion_limit') else 1):
                raise ValueError('invalid '+key)
        if type(self.offline) is not bool:
            raise ValueError('offline must be boolean')
