"""Strict proposal contracts and root-contained code references."""
import hashlib
import math
from pathlib import Path
from src.ideation.pool import Candidate, Review
from src.ideation.roles import RoleOutputError, _object, _citations


def _string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise RoleOutputError(f'{name} must be nonempty text')
    return value


def _strings(value, name):
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise RoleOutputError(f'{name} must be strings')
    return list(value)


def _number(value, name, upper=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (upper is not None and value > upper):
        raise RoleOutputError(f'invalid {name}')
    return value


def safe_path(value, root):
    if not isinstance(value, str) or not value or Path(value).is_absolute() or '..' in Path(value).parts:
        return False
    resolved = (root / value).resolve()
    return resolved.is_relative_to(root.resolve()) and resolved.exists()


def parse_plan(raw):
    _object(raw)
    result = {key: _string(raw.get(key), key) for key in ('objective','compute_note')}
    for key in ('preferences','constraints','idea_attributes','focus_axes'):
        result[key] = _strings(raw.get(key), key)
    if not 3 <= len(result['focus_axes']) <= 6 or any(not v.strip() for v in result['focus_axes']):
        raise RoleOutputError('focus_axes needs 3-6 directions')
    hint = raw.get('domain_hint')
    if hint is not None: _string(hint, 'domain_hint')
    result['domain_hint'] = hint
    return result


def parse_codebase(raw, root: Path):
    _object(raw)
    result = {'summary': _string(raw.get('summary'), 'summary')}
    for key, label in (('entrypoints','purpose'), ('components','role'), ('extension_points','description')):
        rows = raw.get(key)
        if not isinstance(rows, list): raise RoleOutputError(f'invalid {key}')
        result[key] = []
        for row in rows:
            _object(row)
            if safe_path(row.get('path'), root):
                result[key].append({'path': row['path'], label: _string(row.get(label), label)})
    return result


def normalize_proposal(raw, *, cycle, codebase_root: Path | None):
    _object(raw)
    result = {key: _string(raw.get(key), key) for key in
              ('title','statement','mechanism','anticipated_outcomes','novelty_argument')}
    for key, names in (('falsifier', ('description','measurement')),
                       ('first_experiment', ('description','scale','success_criterion'))):
        obj = _object(raw.get(key))
        result[key] = {name: _string(obj.get(name), name) for name in names}
    result['first_experiment']['compute_estimate_gpu_hours'] = _number(
        raw['first_experiment'].get('compute_estimate_gpu_hours'), 'compute_estimate_gpu_hours')
    result['prediction'] = {'confidence': _number(_object(raw.get('prediction')).get('confidence'), 'confidence', 1)}
    result['risks'] = _strings(raw.get('risks'), 'risks')
    paths = _strings(raw.get('codebase_touchpoints', []), 'codebase_touchpoints')
    result['codebase_touchpoints'] = [p for p in paths if codebase_root is not None and safe_path(p, codebase_root)]
    result['rejected_touchpoints'] = len(paths) - len(result['codebase_touchpoints'])
    if codebase_root is not None and not result['codebase_touchpoints']:
        raise RoleOutputError('proposal needs an existing codebase touchpoint')
    result['idea_id'] = 'cand_' + hashlib.sha1((result['title']+'\n'+result['statement']).encode()).hexdigest()[:10]
    return result


def parse_proposal_candidate(raw, *, cycle, strategy, allowed, codebase_root=None):
    bundle = normalize_proposal(raw, cycle=cycle, codebase_root=codebase_root)
    citations, rejected = _citations(raw, allowed)
    bundle['rejected_citations'] = rejected
    parents = _strings(raw.get('parent_ids', []), 'parent_ids')
    return Candidate(bundle['idea_id'], cycle, strategy, tuple(parents), bundle, citations, str(raw.get('rationale','')))


def parse_verification(raw, *, candidate_id, allowed):
    _object(raw)
    assumptions = raw.get('assumptions')
    if not isinstance(assumptions, list) or not assumptions: raise RoleOutputError('assumptions required')
    cleaned = []
    for assumption in assumptions:
        _object(assumption)
        if assumption.get('status') not in ('plausible','uncertain','implausible'): raise RoleOutputError('unknown assumption status')
        cleaned.append({key: _string(assumption.get(key), key) for key in ('claim','status','rationale')})
    weakest = _string(raw.get('weakest'), 'weakest')
    if weakest not in [a['claim'] for a in cleaned]: raise RoleOutputError('weakest must name an assumption claim')
    citations, rejected = _citations(raw, allowed)
    return Review(candidate_id, 'pass', str(raw.get('summary','')), citations, rejected,
                  kind='verification', assumptions=tuple(cleaned), weakest=weakest,
                  kill_experiment=_string(raw.get('kill_experiment'), 'kill_experiment'))
