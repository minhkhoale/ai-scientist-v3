"""Local paper inputs and section/paragraph-level review provenance."""
from dataclasses import replace
import json
from pathlib import Path
import re
import subprocess
from src.ideation.literature.sources import _record, _bib_fields, SourceSpec
from .models import Document, Passage, identifier, ValidationError


def pdf_text(path):
    try:
        result = subprocess.run(['pdftotext','-raw',str(path),'-'],capture_output=True,timeout=90,check=True)
        return result.stdout.decode('utf-8','replace')
    except (OSError,subprocess.SubprocessError) as exc:
        raise ValidationError('PDF text extraction unavailable: '+str(path)) from exc


def make_record(row, source='operator'):
    return _record(row,SourceSpec('folder',source))


def passages(text, source_id, *, aliases=None, max_chars=3500):
    aliases = aliases or {}
    section = 'Body';paragraph = 0;result = []
    # Normalize TeX section markup, preserving all paragraph text/citation syntax.
    text = re.sub(r'\\(?:sub)*section\*?\{([^}]+)\}',r'\n\n## \1\n\n',text)
    for block in re.split(r'\n\s*\n',text):
        lines = block.strip().splitlines()
        if not lines:continue
        body=[]
        for line in lines:
            heading = re.match(r'^\s*#{1,6}\s+(.+)$',line)
            if heading:
                section=heading[1];paragraph=0
            else:body.append(line)
        content='\n'.join(body).strip()
        if not content:continue
        paragraph+=1
        for offset in range(0,len(content),max_chars):
            chunk=content[offset:offset+max_chars]
            keys = re.findall(r'@([\w:.-]+)',chunk)
            keys += [k.strip() for group in re.findall(r'\\cite\w*\{([^}]+)\}',chunk) for k in group.split(',')]
            keys += re.findall(r'\[(L[a-zA-Z0-9]+)\]',chunk)
            keys += [k.strip() for group in re.findall(r'\[(\d+(?:\s*,\s*\d+)*)\]',chunk) for k in group.split(',')]
            cited=tuple(sorted({aliases[k] for k in keys if k in aliases}))
            unresolved=tuple(sorted({k for k in keys if k not in aliases}))
            id=identifier('p_',source_id+'\n'+section+f'\n{paragraph}\n{offset}\n'+chunk)
            result.append(Passage(id,section,paragraph,chunk,cited,unresolved))
    return tuple(result)


def load_inputs(inputs, manuscript, *, pdf_text=pdf_text, lookup=None, max_chars=3500):
    docs=[];aliases={};seen=set()
    def add(row,body='',names=(),status='abstract_only'):
        record=make_record(row)
        for name in (record.id,record.arxiv_id,*names):
            if name:aliases[name]=record.id
        if record.id in seen:return
        seen.add(record.id)
        docs.append(Document(record,'paper',passages(body or record.abstract,record.id,max_chars=max_chars),tuple(names),status))
    def file(path):
        if path.suffix.lower()=='.bib':
            for entry in re.split(r'(?=@\w+\s*\{)',path.read_text()):
                fields=_bib_fields(entry)
                if not fields:continue
                row=fields[0]
                if row.get('doi') and not row.get('url'):row['url']='https://doi.org/'+row['doi']
                key=re.search(r'@\w+\s*\{\s*([^,]+)',entry)
                add(row,names=(key[1],) if key else ())
        elif path.suffix.lower()=='.pdf':
            body=pdf_text(path);add({'title':path.stem,'url':path.resolve().as_uri()},body,(path.stem,),'fulltext_pdf')
        elif path.suffix.lower() in ('.md','.markdown','.tex','.txt'):
            body=path.read_text()
            if path.suffix.lower()=='.txt' and lookup:
                for value in body.splitlines():
                    if value.strip():paper=lookup(value.strip());add(paper.record.__dict__,names=(value.strip(),))
                return
            heading=re.search(r'^#\s+(.+)$',body,re.M)
            add({'title':heading[1] if heading else path.stem,'url':path.resolve().as_uri()},body,(path.stem,),'fulltext_markdown')
        else:raise ValidationError('unsupported input format: '+str(path))
    for value in inputs:
        path=Path(value).expanduser()
        if path.is_dir():
            metadata=path/'records.json'
            if metadata.is_file():
                rows=json.loads(metadata.read_text())
                if isinstance(rows,dict):rows=rows.get('records',[])
                if not isinstance(rows,list):raise ValidationError('invalid records.json')
                for row in rows:
                    if not isinstance(row,dict):raise ValidationError('invalid record row')
                    body='';status='abstract_only';rel=row.get('pdf_path')
                    if rel and isinstance(rel,str):
                        candidate=(path/rel).resolve()
                        if candidate.is_relative_to(path.resolve()) and candidate.is_file():
                            body=pdf_text(candidate);status='fulltext_pdf'
                    names=tuple(str(row[k]) for k in ('id','citation_key','collection_id') if row.get(k))
                    add(row,body,names,status)
            else:
                for child in sorted(path.rglob('*')):
                    if child.is_file() and child.resolve().is_relative_to(path.resolve()) and child.suffix.lower() in ('.md','.pdf','.bib'):
                        file(child)
        elif path.is_file():file(path)
        elif lookup:
            paper=lookup(str(value));add(paper.record.__dict__,names=(str(value),))
        else:raise ValidationError('cannot load paper input: '+str(value))
    manuscript=Path(manuscript)
    body=pdf_text(manuscript) if manuscript.suffix.lower()=='.pdf' else manuscript.read_text()
    # Resolve bibliography aliases against seed titles/URLs, without trusting invented ids.
    for block in re.split(r'\n\s*\n',body):
        for line in block.splitlines():
            for key in re.findall(r'\[@([\w:.-]+)\]|^\s*\[(\d+)\]',line):
                key=next(k for k in key if k)
                for d in docs:
                    if (d.record.url and d.record.url in line) or d.record.title.casefold() in line.casefold():
                        aliases[key]=d.record.id
    record=make_record({'title':'Operator review: '+manuscript.stem,'url':manuscript.resolve().as_uri()})
    docs.append(Document(record,'manuscript',passages(body,record.id,aliases=aliases,max_chars=max_chars),(), 'fulltext_pdf' if manuscript.suffix.lower()=='.pdf' else 'fulltext_markdown'))
    # Resolve local source aliases in paper text too.
    return [replace(d,passages=tuple(replace(p,citations=tuple(sorted(set(p.citations)|{aliases[k] for k in d.aliases if k in aliases and aliases[k]!=d.record.id}))) for p in d.passages)) if d.origin=='paper' else d for d in docs]
