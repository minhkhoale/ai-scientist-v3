"""Bounded role inputs retain required supporting spans and use lexical retrieval."""
from dataclasses import replace
import re
from src.ideation.literature.sources import LiteratureRecord
from src.ideation.literature.retrieval import Bm25Index


def select_claims(claims,query,limit):
    records=[LiteratureRecord(c.id,c.statement) for c in claims]
    ranked=Bm25Index(records).search(query,limit)
    ids=[r.id for r in ranked]
    ids.extend(c.id for c in claims if c.id not in ids)
    mapping={c.id:c for c in claims}
    return tuple(mapping[id] for id in ids[:limit])


def bounded_documents(documents,query,*,required=(),limit=48):
    words=set(re.findall(r'\w+',query.casefold()))
    keep={(e.source,e.passage) for e in required}
    candidates=[]
    for d in documents:
        for p in d.passages:
            score=len(words&set(re.findall(r'\w+',p.text.casefold())))
            candidates.append((-score,d.record.id,p.id))
    for _,source,passage in sorted(candidates):
        if len(keep)>=limit:break
        keep.add((source,passage))
    return [replace(d,passages=tuple(p for p in d.passages if (d.record.id,p.id) in keep)) for d in documents]
