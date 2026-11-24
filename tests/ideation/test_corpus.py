import hashlib
import json
import pytest
from src.ideation.literature.corpus import build_corpus, load_corpus
from src.ideation.literature.sources import LiteratureRecord, LiteratureSourceError


def test_build_writes_sorted_corpus_and_manifest(tmp_path):
    records=[LiteratureRecord('L2','Two',arxiv_id='a'),LiteratureRecord('L1','One'),LiteratureRecord('L3','Duplicate',arxiv_id='a')]
    manifest=build_corpus(tmp_path,['arxiv:q'],require_nonempty=True,fetchers={'arxiv':lambda _:records})
    m=json.loads(manifest.read_text())
    assert [r.id for r in load_corpus(tmp_path)]==['L1','L2']
    assert m['corpus_sha256']==hashlib.sha256((tmp_path/'corpus.jsonl').read_bytes()).hexdigest()


def test_build_is_noop_when_sources_unchanged(tmp_path):
    f={'arxiv':lambda _:[LiteratureRecord('L1','One')]}
    p=build_corpus(tmp_path,['arxiv:q'],require_nonempty=True,fetchers=f); before=p.stat().st_mtime_ns
    build_corpus(tmp_path,['arxiv:q'],require_nonempty=True,fetchers={'arxiv':lambda _:pytest.fail('refetch')})
    assert p.stat().st_mtime_ns==before


def test_build_rejects_changed_sources_on_existing_corpus(tmp_path):
    build_corpus(tmp_path,[],require_nonempty=False)
    with pytest.raises(LiteratureSourceError,match='different sources'): build_corpus(tmp_path,['arxiv:q'],require_nonempty=False)


def test_partial_failure_records_error(tmp_path):
    def fail(_): raise LiteratureSourceError('429')
    p=build_corpus(tmp_path,['arxiv:q','folder:/x'],require_nonempty=True,fetchers={'arxiv':fail,'folder':lambda _:[LiteratureRecord('L1','One'),LiteratureRecord('L2','Two')]})
    m=json.loads(p.read_text()); assert len(m['errors'])==1 and m['record_count']==2


def test_build_fails_when_all_sources_empty(tmp_path):
    with pytest.raises(LiteratureSourceError) as e: build_corpus(tmp_path,['arxiv:a','arxiv:b'],require_nonempty=True,fetchers={'arxiv':lambda _:[]})
    assert 'arxiv:a' in str(e.value) and 'arxiv:b' in str(e.value)


def test_cli_build_folder(tmp_path):
    from src.agsci.main import main
    folder=tmp_path/'source'; folder.mkdir(); (folder/'p.md').write_text('# Paper')
    assert main(['literature','build','--source',f'folder:{folder}','--out',str(tmp_path/'out')])==0
    assert len(load_corpus(tmp_path/'out'))==1
