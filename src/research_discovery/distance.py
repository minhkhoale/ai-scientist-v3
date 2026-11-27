"""Gap-distance profile: how far a verified gap lies from existing work.

Four separate dimensions, never collapsed into the value score:
- SCD, solution coverage distance: 1 - max over prior methods of facet coverage, as an interval
  because a facet the corpus does not show (for example unread experiments) is unknown, not absent;
- SFD, structural frontier distance: the changes the closest method needs (from the coverage role)
  and the cheapest typed graph path from established methods to the gap's failure/assumption nodes;
- SND, semantic nearest-neighbour distance: 1 - max cosine to any corpus paper (cheap screening only);
- confidence: how much of the coverage matrix is known, search completeness, and full-text share.
"""
from dataclasses import dataclass
import heapq
import math
import re
from collections import Counter
from typing import Callable, Sequence

from .models import Evidence, ValidationError, object_fields, supported_evidence, text

FACETS = ('problem', 'setting', 'failure', 'resolution')
WEIGHTS = {'problem': 0.2, 'setting': 0.2, 'failure': 0.3, 'resolution': 0.3}
LEVELS = {'covered': (1.0, 1.0), 'partial': (0.5, 0.5), 'not_covered': (0.0, 0.0), 'unknown': (0.0, 1.0)}
CHANGE_COSTS = {'relax_assumption': 1.0, 'new_setting': 1.0, 'new_representation': 1.5,
                'new_mechanism': 1.5, 'new_capability': 1.5, 'demonstrate': 0.5}
EDGE_COSTS = {'handles': 0.5, 'addresses': 1.0, 'assumes': 1.0, 'fails_under': 1.0,
              'extends': 1.0, 'compares': 1.0, 'evaluated_on': 1.5}
MAX_METHODS = 6


@dataclass(frozen=True)
class Cell:
    level: str
    evidence: tuple[Evidence, ...]
    source: str  # 'agent' or 'graph'


@dataclass(frozen=True)
class Change:
    kind: str
    description: str


@dataclass(frozen=True)
class MethodCoverage:
    method_id: str
    name: str
    cells: dict
    coverage_low: float
    coverage_high: float
    required_changes: tuple[Change, ...]


@dataclass(frozen=True)
class GapDistance:
    gap_id: str
    facets: dict
    scd_low: float
    scd_high: float
    closest_method: str
    resolving_method: str
    methods: tuple[MethodCoverage, ...]
    sfd_transform: float | None
    sfd_changes: tuple[Change, ...]
    sfd_graph: float | None
    snd: float
    snd_neighbor: str
    snd_method: str
    confidence: dict


def _tokens(value):
    return re.findall(r'[a-z0-9]+', value.casefold())


def lexical_embedder(texts: Sequence[str]) -> list[dict]:
    """TF-IDF vectors; the default because no embedding model is installed."""
    docs = [Counter(_tokens(t)) for t in texts]
    df = Counter(word for doc in docs for word in doc)
    n = len(docs)
    vectors = []
    for doc in docs:
        vec = {w: c * math.log((1 + n) / (1 + df[w])) for w, c in doc.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({w: v / norm for w, v in vec.items()})
    return vectors


def _cosine(a, b):
    if isinstance(a, dict):
        return sum(v * b.get(w, 0.0) for w, v in a.items())
    return sum(x * y for x, y in zip(a, b))


def semantic_distance(gap_text, papers, embed: Callable = lexical_embedder):
    """SND = 1 - max cosine(gap, paper). Returns (distance, nearest paper id)."""
    if not papers:
        return 1.0, ''
    vectors = embed([gap_text, *[f'{p.record.title}. {p.record.abstract}' for p in papers]])
    scores = [(_cosine(vectors[0], v), p.record.id) for v, p in zip(vectors[1:], papers)]
    best, nearest = max(scores)
    return round(1.0 - max(0.0, min(1.0, best)), 4), nearest


def candidate_methods(gap, graph, verification=None, limit=MAX_METHODS):
    """Prior methods for the gap's problems, plus methods from papers verification cited against it."""
    nodes = {n.id: n for n in graph.nodes}
    cited = set(gap.graph_node_ids)
    problems = {i for i in cited if i in nodes and nodes[i].type == 'problem'}
    counter_papers = set(verification.resolving_sources) | {e.source for e in verification.counter_evidence} \
        if verification else set()
    found = {e.source for e in graph.edges if e.type == 'addresses' and e.target in problems}
    found |= {i for i in cited if i in nodes and nodes[i].type == 'method'}
    found |= {e.source for e in graph.edges if e.source_paper in counter_papers
              and e.source in nodes and nodes[e.source].type == 'method'}
    papers = {}
    for e in graph.edges:
        if e.source in found:
            papers.setdefault(e.source, set()).add(e.source_paper)
    # Most-supported methods first; counter-evidence methods are kept because they are the likeliest resolvers.
    ranked = sorted(found, key=lambda m: (-(bool(papers.get(m, set()) & counter_papers)), -len(papers.get(m, ())), m))
    return [nodes[m] for m in ranked[:limit] if m in nodes]


def graph_priors(gap, graph, methods):
    """Facet cells the graph already establishes, with the edge's quote as evidence."""
    nodes = {n.id: n for n in graph.nodes}
    cited = set(gap.graph_node_ids)
    priors = {}
    for method in methods:
        cells = {}
        for e in graph.edges:
            if e.source != method.id or e.target not in cited:
                continue
            kind = nodes.get(e.target).type if e.target in nodes else ''
            if e.type == 'addresses' and kind == 'problem':
                cells['problem'] = Cell('covered', e.evidence, 'graph')
            elif e.type == 'evaluated_on' and kind == 'setting':
                cells['setting'] = Cell('covered', e.evidence, 'graph')
            elif e.type == 'handles' and kind in ('failure_condition', 'assumption'):
                cells['failure'] = Cell('covered', e.evidence, 'graph')
            elif e.type in ('fails_under', 'assumes') and kind in ('failure_condition', 'assumption'):
                cells.setdefault('failure', Cell('not_covered', e.evidence, 'graph'))
        priors[method.id] = cells
    return priors


def parse_coverage(raw, *, methods, priors, documents):
    """Validate the coverage role. Positive levels need a checked quote; a negative level needs a quote
    or a negative graph edge; anything else becomes unknown rather than counted as absence."""
    object_fields(raw, ('facets', 'methods'))
    facets_raw = object_fields(raw['facets'], FACETS)
    facets = {f: text(facets_raw[f]) for f in FACETS}
    allowed = {m.id: m for m in methods}
    rows = {}
    if not isinstance(raw['methods'], list):
        raise ValidationError('coverage methods must be a list')
    for item in raw['methods']:
        object_fields(item, ('method_id', 'cells', 'required_changes'))
        method_id = item['method_id']
        if method_id not in allowed or method_id in rows:
            continue
        cells_raw = object_fields(item['cells'], FACETS)
        cells = {}
        for facet in FACETS:
            cell = object_fields(cells_raw[facet], ('level', 'evidence'))
            level = cell['level']
            if level not in LEVELS:
                raise ValidationError('unknown coverage level')
            spans = supported_evidence(cell['evidence'], documents)
            prior = priors.get(method_id, {}).get(facet)
            if level in ('covered', 'partial') and not spans:
                level = 'unknown'
            if level == 'not_covered' and not spans and not (prior and prior.level == 'not_covered'):
                level = 'unknown'
            if level == 'unknown' and prior is not None:
                cells[facet] = prior
                continue
            cells[facet] = Cell(level, spans, 'agent')
        changes = []
        if not isinstance(item['required_changes'], list):
            raise ValidationError('required_changes must be a list')
        for change in item['required_changes']:
            object_fields(change, ('kind', 'description'))
            if change['kind'] not in CHANGE_COSTS:
                raise ValidationError('unknown change kind')
            changes.append(Change(change['kind'], text(change['description'])))
        low = sum(WEIGHTS[f] * LEVELS[cells[f].level][0] for f in FACETS)
        high = sum(WEIGHTS[f] * LEVELS[cells[f].level][1] for f in FACETS)
        rows[method_id] = MethodCoverage(method_id, allowed[method_id].name, cells, round(low, 4), round(high, 4),
                                         tuple(changes))
    # Methods the role skipped keep only their graph priors.
    for method in methods:
        if method.id not in rows:
            cells = {f: priors.get(method.id, {}).get(f, Cell('unknown', (), 'graph')) for f in FACETS}
            low = sum(WEIGHTS[f] * LEVELS[cells[f].level][0] for f in FACETS)
            high = sum(WEIGHTS[f] * LEVELS[cells[f].level][1] for f in FACETS)
            rows[method.id] = MethodCoverage(method.id, method.name, cells, round(low, 4), round(high, 4), ())
    return facets, tuple(rows[m.id] for m in methods)


def graph_frontier_distance(gap, graph, method_ids, hub_quantile=0.95):
    """Cheapest typed path from a candidate method to the gap's failure/assumption (else problem) nodes.
    Paper and finding nodes are not traversed; high-degree hub nodes cost extra."""
    nodes = {n.id: n for n in graph.nodes}
    cited = [i for i in gap.graph_node_ids if i in nodes]
    targets = {i for i in cited if nodes[i].type in ('failure_condition', 'assumption')} or \
              {i for i in cited if nodes[i].type == 'problem'}
    if not targets or not method_ids:
        return None
    adjacency, degree = {}, Counter()
    for e in graph.edges:
        if e.type not in EDGE_COSTS or e.source not in nodes or e.target not in nodes:
            continue
        adjacency.setdefault(e.source, []).append((e.target, EDGE_COSTS[e.type]))
        adjacency.setdefault(e.target, []).append((e.source, EDGE_COSTS[e.type]))
        degree.update((e.source, e.target))
    cutoff = sorted(degree.values())[int(hub_quantile * (len(degree) - 1))] if degree else 0
    queue = [(0.0, m) for m in method_ids]
    best = {m: 0.0 for m in method_ids}
    while queue:
        cost, node = heapq.heappop(queue)
        if node in targets:
            return round(cost, 3)
        if cost > best.get(node, math.inf):
            continue
        for neighbor, step in adjacency.get(node, ()):
            extra = 2.0 if degree[neighbor] > cutoff and neighbor not in targets else 0.0
            new = cost + step + extra
            if new < best.get(neighbor, math.inf):
                best[neighbor] = new
                heapq.heappush(queue, (new, neighbor))
    return None


def gap_distance(gap, graph, verification, documents, coverage, facets, *, embed=lexical_embedder,
                 embed_name='lexical TF-IDF cosine (no embedding model installed)'):
    methods = coverage
    if methods:
        closest = max(methods, key=lambda m: ((m.coverage_low + m.coverage_high) / 2, m.coverage_low, m.method_id))
        scd_low = round(1 - max(m.coverage_high for m in methods), 4)
        scd_high = round(1 - max(m.coverage_low for m in methods), 4)
        resolving = next((m.method_id for m in methods if all(m.cells[f].level == 'covered' for f in FACETS)), '')
        transform = sum(CHANGE_COSTS[c.kind] for c in closest.required_changes) if closest.required_changes else None
        changes = closest.required_changes
        known = sum(m.cells[f].level != 'unknown' for m in methods for f in FACETS) / (len(methods) * len(FACETS))
    else:
        closest, scd_low, scd_high, resolving, transform, changes, known = None, 0.0, 1.0, '', None, (), 0.0
    papers = [d for d in documents if d.origin == 'paper']
    snd, neighbor = semantic_distance(gap.statement, papers, embed)
    searches = verification.searches
    cited_papers = {e.source for m in methods for f in FACETS for e in m.cells[f].evidence}
    fulltext = [d for d in papers if d.record.id in cited_papers]
    confidence = {
        'known_cell_fraction': round(known, 3),
        'search_coverage': round(sum(s.status == 'ok' for s in searches) / len(searches), 3) if searches else 0.0,
        'supporting_papers': len(set(gap.closest_prior)),
        'fulltext_fraction': round(sum(d.fulltext_status != 'abstract_only' for d in fulltext) / len(fulltext), 3)
                             if fulltext else 0.0,
        'verdict': verification.verdict,
    }
    return GapDistance(gap.id, facets, scd_low, scd_high, closest.method_id if closest else '', resolving,
                       tuple(methods), transform, tuple(changes),
                       graph_frontier_distance(gap, graph, [m.method_id for m in methods]),
                       snd, neighbor, embed_name, confidence)
