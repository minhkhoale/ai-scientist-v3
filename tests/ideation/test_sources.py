import json
from datetime import date
from pathlib import Path
import pytest
from src.ideation.literature.sources import (parse_source_spec, fetch_folder, fetch_arxiv, fetch_conference, LiteratureSourceError, HttpStatusError)


def test_parse_specs():
    s = parse_source_spec('iclr:2025,2026:diffusion polic|max=40')
    assert (s.kind,s.venue,s.years,s.query,s.max_results)==('conference','ICLR',(2025,2026),'diffusion polic',40)
    s = parse_source_spec('arxiv:video diffusion|from=2025-01-01|cat=cs.CV,cs.LG')
    assert s.date_from==date(2025,1,1) and s.categories==('cs.CV','cs.LG')
    assert parse_source_spec('folder:/x').path==Path('/x')
    for bad in ['cvpr:20x:q','arxiv:','bogus:q','arxiv:a|oops=2','folder:','arxiv:q|max=0']:
        with pytest.raises(LiteratureSourceError): parse_source_spec(bad)


def test_folder_sputnik_records(tmp_path):
    rows=[{'title':'One','url':'https://one','abstract':'A'},{'title':'Two','url':'https://two'}]
    (tmp_path/'records.json').write_text(json.dumps(rows))
    (tmp_path/'ignored.md').write_text('# Ignored')
    assert [r.title for r in fetch_folder(parse_source_spec(f'folder:{tmp_path}'))]==['One','Two']


def test_folder_markdown_only(tmp_path):
    d=tmp_path/'nested'; d.mkdir(); (d/'paper.md').write_text('# Paper\n## Abstract\nA useful abstract\n## Other\nignored')
    r=fetch_folder(parse_source_spec(f'folder:{tmp_path}'))[0]
    assert r.title=='Paper' and r.abstract=='A useful abstract'


def test_folder_bib_only(tmp_path):
    (tmp_path/'references.bib').write_text('@article{p, title={A {Nested} Title}, abstract={An abstract}, author={Alice and Bob}, year={2025}, url={https://paper}}')
    r=fetch_folder(parse_source_spec(f'folder:{tmp_path}'))[0]
    assert r.title=='A Nested Title' and r.abstract=='An abstract' and r.year==2025 and r.authors==('Alice','Bob')


def test_folder_pdfs_only_errors(tmp_path):
    (tmp_path/'paper.pdf').touch()
    with pytest.raises(LiteratureSourceError,match='PDFs are not parsed') as e: fetch_folder(parse_source_spec(f'folder:{tmp_path}'))
    assert str(tmp_path) in str(e.value)


def test_arxiv_parses_feed_and_filters_date():
    feed=b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2501.01234v2</id><title>Video</title><summary>diffusion</summary><published>2025-02-01T00:00:00Z</published><author><name>A</name></author></entry><entry><id>http://arxiv.org/abs/2401.01234</id><title>Old</title><published>2024-01-01T00:00:00Z</published></entry></feed>'''
    urls=[]; sleeps=[]
    def get(url): urls.append(url); return feed if len(urls)==1 else b'<feed xmlns="http://www.w3.org/2005/Atom"/>'
    records=fetch_arxiv(parse_source_spec('arxiv:video diffusion|from=2025-01-01|max=101'),http_get=get,sleep=sleeps.append)
    assert len(records)==1 and records[0].arxiv_id=='2501.01234'
    assert 'search_query=all:video+AND+all:diffusion' in urls[0] and 'sortBy=submittedDate' in urls[0]
    # Fewer than a page means there are no further results.
    assert not sleeps


def test_arxiv_pagination_waits():
    entry='<entry><id>http://arxiv.org/abs/2501.00001</id><title>A</title><published>2025-01-01</published></entry>'
    replies=iter([('<feed xmlns="http://www.w3.org/2005/Atom">'+entry*100+'</feed>').encode(), b'<feed xmlns="http://www.w3.org/2005/Atom"/>'])
    sleeps=[]; fetch_arxiv(parse_source_spec('arxiv:q|max=101'),http_get=lambda _: next(replies),sleep=sleeps.append)
    assert sleeps==[3.0]


def test_conference_filters_fragments():
    rows={'rows':[{'row':{'title':'Diffusion policies','abstract':'robot','year':2025},'truncated_cells':[]},{'row':{'title':'Diffusion','abstract':'missing'}},{'row':{'title':'Diffusion policy'},'truncated_cells':['abstract']}], 'num_rows_total':3}
    urls=[]
    def get(url): urls.append(url); return json.dumps(rows).encode()
    rs=fetch_conference(parse_source_spec('iclr:2025:diffusion polic'),http_get=get,sleep=lambda _:None)
    assert len(rs)==1 and '/search?' in urls[0] and 'dataset=ai-conferences%2FICLR2025' in urls[0]


def test_conference_retries_429_then_records_error():
    sleeps=[]
    def get(_): raise HttpStatusError(429)
    with pytest.raises(LiteratureSourceError,match='429'): fetch_conference(parse_source_spec('iclr:2025:q'),http_get=get,sleep=sleeps.append)
    assert sleeps==[2,4,8,16,32]
