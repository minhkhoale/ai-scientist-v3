"""Immutable source graph and strict, quote-checked boundary primitives."""
from dataclasses import dataclass
import hashlib
from typing import Any
from src.ideation.literature.sources import LiteratureRecord


class ValidationError(ValueError):
    pass


class UnsupportedEvidence(ValidationError):
    pass


def object_fields(raw, required, optional=()):
    if not isinstance(raw, dict) or set(raw)-set(required)-set(optional) or set(required)-set(raw):
        raise ValidationError('invalid object fields: '+str(required))
    return raw


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError('expected nonempty text')
    return value


def array(value):
    if not isinstance(value, list):
        raise ValidationError('expected a list')
    return value


def identifier(prefix, value):
    return prefix+hashlib.sha256(value.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Passage:
    id: str
    section: str
    paragraph: int
    text: str
    citations: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True)
class Document:
    record: LiteratureRecord
    origin: str
    passages: tuple[Passage, ...]
    aliases: tuple[str, ...] = ()
    fulltext_status: str = 'abstract_only'


@dataclass(frozen=True)
class Evidence:
    source: str
    passage: str
    start: int
    end: int
    quote: str


def evidence(raw, documents):
    try:
        object_fields(raw, ('source','passage','start','end','quote'))
        doc = next(d for d in documents if d.record.id == raw['source'])
        passage = next(p for p in doc.passages if p.id == raw['passage'])
        start, end = raw['start'], raw['end']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(passage.text):
            raise ValidationError('invalid quote offsets')
        quote = text(raw['quote'])
        if passage.text[start:end] != quote:
            raise ValidationError('quote differs from source span')
        return Evidence(doc.record.id, passage.id, start, end, quote)
    except (ValidationError, StopIteration, TypeError, KeyError) as exc:
        raise UnsupportedEvidence('unsupported quote/source/passage') from exc


@dataclass(frozen=True)
class Claim:
    id: str
    kind: str
    statement: str
    evidence: tuple[Evidence, ...]
    uncertainty: str
    citations: tuple[str, ...]


@dataclass(frozen=True)
class Extraction:
    claims: tuple[Claim, ...]
    dropped: tuple[str, ...]


def document_from_dict(raw: dict[str, Any]) -> Document:
    record = dict(raw['record']);record['authors'] = tuple(record['authors'])
    passages = tuple(Passage(**{**p,'citations':tuple(p['citations']),'unresolved':tuple(p['unresolved'])}) for p in raw['passages'])
    return Document(LiteratureRecord(**record),raw['origin'],passages,tuple(raw['aliases']),raw['fulltext_status'])
