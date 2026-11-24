"""Host-side literature sources. HTTP and timing seams permit offline tests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Literal
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class LiteratureSourceError(ValueError):
    pass


class HttpStatusError(OSError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f'HTTP {status}')


@dataclass(frozen=True)
class SourceSpec:
    kind: Literal['folder', 'arxiv', 'conference']
    raw: str
    path: Path | None = None
    query: str = ''
    venue: str | None = None
    years: tuple[int, ...] = ()
    max_results: int = 50
    date_from: date | None = None
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiteratureRecord:
    id: str
    title: str
    abstract: str = ''
    authors: tuple[str, ...] = ()
    year: int | None = None
    venue: str = ''
    url: str = ''
    arxiv_id: str = ''
    source: str = ''


def parse_source_spec(text: str) -> SourceSpec:
    try:
        base, *options = text.split('|')
        kind, rest = base.split(':', 1)
        opts = dict(item.split('=', 1) for item in options)
        allowed = {'arxiv': {'max', 'from', 'cat'}, 'folder': set()}.get(kind, {'max'})
        if set(opts) - allowed or len(opts) != len(options) or not rest.strip():
            raise ValueError('empty source or unknown/duplicate option')
        maximum = int(opts.get('max', 100 if kind not in ('arxiv','folder') else 50))
        if maximum <= 0:
            raise ValueError('max must be positive')
        if kind == 'folder':
            return SourceSpec('folder', text, path=Path(rest).expanduser())
        if kind == 'arxiv':
            cats = tuple(c.strip() for c in opts.get('cat','').split(',') if c.strip())
            return SourceSpec('arxiv', text, query=rest, max_results=maximum,
                              date_from=date.fromisoformat(opts['from']) if 'from' in opts else None, categories=cats)
        venues = {'iclr':'ICLR','cvpr':'CVPR','neurips':'NeurIPS','nips':'NeurIPS','icml':'ICML'}
        years, query = rest.split(':',1)
        if not query.strip() or not re.fullmatch(r'\d{4}(,\d{4})*',years):
            raise ValueError('expected YYYY[,YYYY]:query')
        return SourceSpec('conference',text,query=query,venue=venues[kind],years=tuple(map(int,years.split(','))),max_results=maximum)
    except (ValueError, KeyError, TypeError) as exc:
        raise LiteratureSourceError(f'invalid source {text!r}: {exc}') from exc


def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent':'agentic-scientist-literature/0.1'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise HttpStatusError(response.status)
            return response.read()
    except urllib.error.HTTPError as exc:
        raise HttpStatusError(exc.code) from exc


def _record(row: dict, spec: SourceSpec) -> LiteratureRecord:
    title = str(row.get('title') or '').strip()
    if not title:
        raise LiteratureSourceError(f'{spec.raw}: record without title')
    url = str(row.get('url') or row.get('paper_url') or row.get('pdf_url') or '')
    arxiv = str(row.get('arxiv_id') or '')
    if 'arxiv.org/' in url:
        arxiv = re.sub(r'v\d+$','',url.split('/')[-1].removesuffix('.pdf'))
    authors = row.get('authors') or row.get('author') or ()
    if isinstance(authors,str):
        authors = authors.split(' and ')
    authors = tuple(str(a.get('name','')) if isinstance(a,dict) else str(a) for a in authors)
    try:
        year = int(row['year']) if row.get('year') else None
    except (ValueError,TypeError):
        year = None
    return LiteratureRecord('L'+hashlib.sha1((url or title.lower()).encode()).hexdigest()[:8], title,
                            str(row.get('abstract') or row.get('summary') or ''), authors, year,
                            str(row.get('venue') or row.get('conference') or spec.venue or ''), url, arxiv, spec.raw)


def _bib_fields(text: str) -> list[dict]:
    """Read balanced brace or quoted fields, retaining nested TeX title text."""
    rows: list[dict] = []
    for entry in re.split(r'@\w+\s*\{',text)[1:]:
        row = {}
        pos = entry.find(',')+1
        while match := re.search(r'(\w+)\s*=\s*',entry[pos:]):
            key = match[1].lower(); start = pos+match.end(); pos = start
            if start >= len(entry):
                break
            opener = entry[start]
            if opener == '{':
                depth = 1; pos = start+1
                while pos < len(entry) and depth:
                    depth += (entry[pos]=='{')-(entry[pos]=='}'); pos += 1
                value = entry[start+1:pos-1]
            elif opener == '"':
                pos = entry.find('"',start+1)
                if pos < 0: break
                value = entry[start+1:pos]; pos += 1
            else:
                end = re.search(r'[,}]',entry[start:]); pos = start+end.start() if end else len(entry)
                value = entry[start:pos]
            row[key] = value.replace('{','').replace('}','').strip()
        if 'title' in row:
            rows.append(row)
    return rows


def fetch_folder(spec: SourceSpec) -> list[LiteratureRecord]:
    path = spec.path
    if path is None or not path.is_dir():
        raise LiteratureSourceError(f'{path}: folder does not exist')
    records = sorted(path.rglob('records.json'))
    rows: list[dict] = []
    if records:
        for file in records:
            value = json.loads(file.read_text())
            rows.extend(value if isinstance(value,list) else value.get('records',[]))
    else:
        for file in sorted(path.rglob('*.md')):
            text = file.read_text(); title = re.search(r'^#\s+(.+)$',text,re.M)
            abstract = re.search(r'^##\s+Abstract\s*\n(.*?)(?=^#{1,2}\s|\Z)',text,re.M|re.S|re.I)
            rows.append({'title': title[1] if title else file.stem,'abstract':abstract[1].strip() if abstract else text.strip()})
        for file in sorted(path.rglob('*.bib')):
            rows.extend(_bib_fields(file.read_text()))
    if not rows and any(path.rglob('*.pdf')):
        raise LiteratureSourceError(f'{path}: PDFs are not parsed; provide Markdown or BibTeX')
    return [_record(row,spec) for row in rows]


def fetch_arxiv(spec: SourceSpec, *, http_get: Callable[[str],bytes] = http_get,
                sleep: Callable[[float],None] = time.sleep) -> list[LiteratureRecord]:
    query = ' AND '.join('all:'+word for word in spec.query.split())
    if spec.categories:
        query += ' AND ('+' OR '.join('cat:'+c for c in spec.categories)+')'
    result = []; start = 0
    ns = {'a':'http://www.w3.org/2005/Atom'}
    while start < spec.max_results:
        if start: sleep(3.0)
        params = urllib.parse.urlencode({'search_query':query,'start':start,'max_results':min(100,spec.max_results-start),'sortBy':'submittedDate','sortOrder':'descending'},safe=':')
        entries = ET.fromstring(http_get('http://export.arxiv.org/api/query?'+params)).findall('a:entry',ns)
        for entry in entries:
            def value(key): return (entry.findtext('a:'+key,default='',namespaces=ns)).strip()
            published = value('published')[:10]
            if spec.date_from and (not published or date.fromisoformat(published)<spec.date_from): continue
            result.append(_record({'title':value('title'),'abstract':value('summary'),'url':value('id'),'year':published[:4],
                                   'authors':[a.findtext('a:name',default='',namespaces=ns) for a in entry.findall('a:author',ns)]},spec))
        if len(entries)<min(100,spec.max_results-start): break
        start += len(entries)
    return result


def fetch_conference(spec: SourceSpec, *, http_get: Callable[[str],bytes] = http_get,
                     sleep: Callable[[float],None] = time.sleep) -> list[LiteratureRecord]:
    result: list[LiteratureRecord] = []
    for year in spec.years:
        offset = 0
        while len(result)<spec.max_results:
            params = urllib.parse.urlencode({'dataset':f'ai-conferences/{spec.venue}{year}','config':'default','split':'train','query':spec.query,'offset':offset,'length':100})
            url = 'https://datasets-server.huggingface.co/search?'+params
            for attempt in range(6):
                try:
                    data = json.loads(http_get(url)); break
                except HttpStatusError as exc:
                    if exc.status!=429 or attempt==5:
                        raise LiteratureSourceError(f'{spec.raw}: {exc}') from exc
                    sleep(min(2**(attempt+1),60))
            rows = data.get('rows',[])
            for item in rows:
                if item.get('truncated_cells'): continue
                row = dict(item.get('row',{})); row.setdefault('year',year)
                text = f"{row.get('title','')} {row.get('abstract','')}".lower()
                if all(word.lower() in text for word in spec.query.split()): result.append(_record(row,spec))
                if len(result)>=spec.max_results: break
            offset += len(rows)
            if not rows or offset>=data.get('num_rows_total',offset): break
    return result
