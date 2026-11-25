"""Structured claims retain only exact, source-checked supporting passages."""
from .models import (Extraction, Claim, ValidationError, UnsupportedEvidence, object_fields,
                     text, array, evidence, identifier)


def extract_claims(raw, documents):
    object_fields(raw,('claims',))
    claims={};dropped=[]
    for item in array(raw['claims']):
        object_fields(item,('kind','statement','uncertainty','evidence'))
        if item['kind'] not in ('problem','method','assumption','limitation','finding'):
            raise ValidationError('unknown claim kind')
        if item['uncertainty'] not in ('reported','hypothesis','uncertain'):
            raise ValidationError('unknown uncertainty')
        statement=text(item['statement'])
        try:
            spans=tuple(evidence(e,documents) for e in array(item['evidence']))
            if not spans:raise UnsupportedEvidence('claim lacks evidence')
        except UnsupportedEvidence as exc:
            dropped.append(str(exc));continue
        cited=set();uncertainty=item['uncertainty']
        for span in spans:
            doc=next(d for d in documents if d.record.id==span.source)
            p=next(p for p in doc.passages if p.id==span.passage)
            if doc.origin=='paper':cited.add(span.source)
            cited.update(p.citations)
            if p.unresolved:uncertainty='uncertain'
        id=identifier('claim_',item['kind']+'\n'+statement+'\n'+repr(spans))
        claims[id]=Claim(id,item['kind'],statement,spans,uncertainty,tuple(sorted(cited)))
    return Extraction(tuple(claims.values()),tuple(dropped))
