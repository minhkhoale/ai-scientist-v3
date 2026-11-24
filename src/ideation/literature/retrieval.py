"""BM25 retrieval over the frozen corpus and bounded prompt packs."""
from collections import Counter
import math
from typing import Sequence
from .sources import LiteratureRecord
from ..similarity import tokens


class Bm25Index:
    def __init__(self, records: Sequence[LiteratureRecord], *, k1: float = 1.5, b: float = .75):
        self.records = list(records)
        self.k1,self.b = k1,b
        self.terms = [Counter(tokens(r.title+' '+r.abstract)) for r in records]
        self.lengths = [sum(t.values()) for t in self.terms]
        self.average = sum(self.lengths)/len(records) if records else 1
        self.df = Counter(word for terms in self.terms for word in terms)

    def search(self, query: str, k: int) -> list[LiteratureRecord]:
        words = tokens(query)
        if not words or k<=0: return []
        scores = []
        for record,terms,length in zip(self.records,self.terms,self.lengths):
            score = 0.0
            for word in words:
                frequency = terms[word]
                if not frequency: continue
                inverse = math.log(1+(len(self.records)-self.df[word]+.5)/(self.df[word]+.5))
                score += inverse*frequency*(self.k1+1)/(frequency+self.k1*(1-self.b+self.b*length/(self.average or 1)))
            scores.append((score,record))
        return [r for score,r in sorted(scores,key=lambda x:(-x[0],x[1].id))[:k] if score>0]


def render_pack(records: Sequence[LiteratureRecord], *, max_abstract_chars: int = 1200) -> str:
    return '\n\n'.join(f'### [{r.id}] {r.title} ({r.venue} {r.year or ""})\n{r.url}\n\n{r.abstract[:max_abstract_chars]}' for r in records)


def pack_ids(records: Sequence[LiteratureRecord]) -> frozenset[str]:
    return frozenset(r.id for r in records)
