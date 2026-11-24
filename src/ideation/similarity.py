"""Deterministic lexical similarity without embedding dependencies."""
from collections import Counter
import math
import re
from typing import Mapping, Sequence


def tokens(text: str) -> list[str]:
    return re.findall(r'[a-z0-9]+',text.lower())


def text_similarity(a: str, b: str, corpus: Sequence[str]) -> float:
    documents = [set(tokens(text)) for text in [*corpus,a,b]]
    df = Counter(word for document in documents for word in document)
    def vector(text):
        return {word:count*(1+math.log((1+len(documents))/(1+df[word]))) for word,count in Counter(tokens(text)).items()}
    va,vb = vector(a),vector(b)
    denominator = math.sqrt(sum(v*v for v in va.values())*sum(v*v for v in vb.values()))
    return min(1.0,max(0.0,sum(v*vb.get(w,0) for w,v in va.items())/denominator)) if denominator else 0.0


def find_duplicate(text: str, existing: Mapping[str,str], *, threshold: float) -> str | None:
    scored = [(text_similarity(text,value,list(existing.values())),key) for key,value in sorted(existing.items())]
    score,key = max(scored,default=(0.0,None))
    return key if score>=threshold else None
