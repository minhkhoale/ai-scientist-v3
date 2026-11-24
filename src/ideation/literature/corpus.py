"""Build a per-run, immutable literature snapshot."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Mapping, Sequence
from .sources import (LiteratureRecord, LiteratureSourceError, SourceSpec, parse_source_spec,
                      fetch_folder, fetch_arxiv, fetch_conference)


def _atomic_write(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.'+path.name)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def build_corpus(out_dir: Path, specs: Sequence[str], *, require_nonempty: bool,
                 fetchers: Mapping[str,Callable[[SourceSpec],list[LiteratureRecord]]] | None = None) -> Path:
    manifest_path = out_dir/'manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest['sources'] != list(specs):
            raise LiteratureSourceError('corpus already built from different sources')
        existing_records = load_corpus(out_dir)
        if require_nonempty and not existing_records:
            raise LiteratureSourceError('empty corpus: '+', '.join(specs))
        return manifest_path
    fetchers = fetchers or {'folder':fetch_folder,'arxiv':fetch_arxiv,'conference':fetch_conference}
    counts = {}; errors = {}; records: dict[str, LiteratureRecord] = {}; arxiv_ids = set()
    for raw in specs:
        spec = parse_source_spec(raw)
        try:
            rows = fetchers[spec.kind](spec); counts[raw] = len(rows)
            for record in rows:
                if record.id in records or (record.arxiv_id and record.arxiv_id in arxiv_ids): continue
                records[record.id] = record
                if record.arxiv_id: arxiv_ids.add(record.arxiv_id)
        except (LiteratureSourceError, OSError, ValueError) as exc:
            errors[raw] = str(exc); counts[raw] = 0
    if require_nonempty and not records:
        raise LiteratureSourceError('empty corpus: '+'; '.join(f'{raw}: {errors.get(raw,"no records")}' for raw in specs))
    out_dir.mkdir(parents=True,exist_ok=True)
    data = ''.join(json.dumps(asdict(records[key]),sort_keys=True)+'\n' for key in sorted(records)).encode()
    manifest = {'schema_version':1,'sources':list(specs),'built_at':datetime.now(timezone.utc).isoformat(),
                'counts':counts,'errors':errors,'corpus_sha256':hashlib.sha256(data).hexdigest(),'record_count':len(records)}
    _atomic_write(out_dir/'corpus.jsonl',data)
    _atomic_write(manifest_path,(json.dumps(manifest,indent=2)+'\n').encode())
    return manifest_path


def load_corpus(out_dir: Path) -> list[LiteratureRecord]:
    manifest = json.loads((out_dir/'manifest.json').read_text())
    data = (out_dir/'corpus.jsonl').read_bytes()
    if hashlib.sha256(data).hexdigest()!=manifest['corpus_sha256']:
        raise LiteratureSourceError(f'{out_dir}: corpus checksum mismatch')
    records = []
    for line in data.decode().splitlines():
        row = json.loads(line); row['authors'] = tuple(row['authors']); records.append(LiteratureRecord(**row))
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='agsci literature')
    commands = parser.add_subparsers(dest='command',required=True)
    build = commands.add_parser('build')
    build.add_argument('--source',action='append',required=True)
    build.add_argument('--out',type=Path,required=True)
    build.add_argument('--allow-empty',action='store_true')
    args = parser.parse_args(argv)
    try:
        path = build_corpus(args.out,args.source,require_nonempty=not args.allow_empty)
    except (LiteratureSourceError, OSError) as exc:
        parser.error(str(exc))
    manifest = json.loads(path.read_text())
    print(json.dumps({'counts':manifest['counts'],'errors':manifest['errors'],'record_count':manifest['record_count']},indent=2))
    return 0
