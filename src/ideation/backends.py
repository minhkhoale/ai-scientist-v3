"""Backend registry: the legacy runner is passed through unchanged by default."""
from pathlib import Path
from typing import Callable, Mapping, Protocol
from src.investigation.models import IdeaGenerationResult
from .literature.corpus import build_corpus, load_corpus
from .literature.sources import LiteratureSourceError, parse_source_spec
from .round import make_tournament_idea_generation, record_outcome


class IdeationBackend(Protocol):
    name: str
    def prepare(self, cfg, *, results_dir: Path, log_event_fn) -> None: ...
    def idea_generation_fn(self, *, default: Callable[...,IdeaGenerationResult]) -> Callable[...,IdeaGenerationResult]: ...
    def record_outcome(self, results_dir: Path, **ledger_fields) -> None: ...


class SakanaScientistV3Backend:
    name = 'sakana-scientist-v3'
    def prepare(self, cfg, *, results_dir: Path, log_event_fn) -> None: pass
    def idea_generation_fn(self, *, default): return default
    def record_outcome(self, results_dir: Path, **ledger_fields) -> None: pass


class CoScientistBackend:
    name = 'coscientist'
    def prepare(self, cfg, *, results_dir: Path, log_event_fn) -> None:
        literature = cfg.ideation.literature
        for source in literature.sources: parse_source_spec(source)
        if literature.corpus_dir:
            self.corpus_dir = Path(literature.corpus_dir).expanduser().resolve()
            records = load_corpus(self.corpus_dir)
            if literature.require_nonempty and not records: raise LiteratureSourceError(f'{self.corpus_dir}: empty corpus')
        else:
            if not literature.sources: raise ValueError('coscientist requires ideation.literature.sources or ideation.literature.corpus_dir')
            self.corpus_dir = (results_dir/'literature').resolve()
            build_corpus(self.corpus_dir,literature.sources,require_nonempty=literature.require_nonempty)
        literature.corpus_dir = str(self.corpus_dir)
        self.cfg,self.log_event_fn = cfg,log_event_fn

    def idea_generation_fn(self, *, default):
        return make_tournament_idea_generation(self.cfg,corpus_dir=self.corpus_dir,log_event_fn=self.log_event_fn)

    def record_outcome(self, results_dir: Path, **ledger_fields):
        record_outcome(results_dir,**ledger_fields)


BACKENDS: Mapping[str,Callable[[],IdeationBackend]] = {'sakana-scientist-v3':SakanaScientistV3Backend,'coscientist':CoScientistBackend}


def get_backend(name: str) -> IdeationBackend:
    if name not in BACKENDS:
        raise ValueError(f'unknown ideation.backend {name!r}; choose one of: '+', '.join(sorted(BACKENDS)))
    return BACKENDS[name]()
