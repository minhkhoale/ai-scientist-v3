"""Typed, quote-grounded research graph primitives.

Design provenance: SGHA (Apache-2.0, https://github.com/SarveshVGharat/structural-gap-hypothesis-agent)
informs the evidence graph and recurring-structure workflow. No SGHA implementation code is copied.
"""
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import math
import re

from .models import Evidence, UnsupportedEvidence, ValidationError, array, evidence, object_fields, text

NODE_TYPES = ('problem', 'method', 'assumption', 'failure_condition', 'setting', 'finding', 'paper')
EDGE_TYPES = ('addresses', 'assumes', 'fails_under', 'evaluated_on', 'extends', 'compares', 'reports', 'contradicts', 'handles')


@dataclass(frozen=True)
class GraphNode:
    id: str
    type: str
    name: str
    aliases: tuple[str, ...] = ()
    paper_id: str = ''
    tier: str = ''


@dataclass(frozen=True)
class GraphEdge:
    id: str
    type: str
    source: str
    target: str
    source_paper: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class ResearchGraph:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    dropped: tuple[str, ...] = ()


@dataclass(frozen=True)
class Relevance:
    paper_id: str
    lexical: float
    manuscript_citations: int
    manuscript_sections: int
    citation_links: float
    total: float
    tier: str


@dataclass(frozen=True)
class GapCandidate:
    pattern: str
    statement: str
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]


def _tokens(value):
    return re.findall(r'[a-z0-9]+', value.casefold())


def rank_relevance(topic, documents, deep_papers=40):
    """Rank papers lexically and by manuscript/corpus links, reserving exploration slots."""
    papers = [d for d in documents if d.origin == 'paper']
    manuscript = [d for d in documents if d.origin == 'manuscript']
    query = set(_tokens(topic))
    citation_sections, citation_count = {}, {}
    for doc in manuscript:
        for passage in doc.passages:
            for paper_id in passage.citations:
                citation_count[paper_id] = citation_count.get(paper_id, 0) + 1
                citation_sections.setdefault(paper_id, set()).add(passage.section)
    term_docs = {d.record.id: _tokens(d.record.title) * 2 + _tokens(d.record.abstract) for d in papers}
    average_length = sum(map(len, term_docs.values())) / max(len(term_docs), 1)
    document_frequency = {term: sum(term in tokens for tokens in term_docs.values()) for term in query}
    lexical = {}
    for doc in papers:
        tokens = term_docs[doc.record.id]
        score = 0.0
        for term in query:
            frequency = tokens.count(term)
            if not frequency:
                continue
            inverse_frequency = math.log(1 + (len(papers) - document_frequency[term] + 0.5) /
                                         (document_frequency[term] + 0.5))
            normalization = frequency + 1.5 * (1 - 0.75 + 0.75 * len(tokens) / max(average_length, 1))
            score += inverse_frequency * frequency * 2.5 / normalization
        lexical[doc.record.id] = score
    manuscript_score = {d.record.id: citation_count.get(d.record.id, 0) + len(citation_sections.get(d.record.id, ()))
                        for d in papers}
    initial = sorted(papers, key=lambda d: (-(lexical[d.record.id] + manuscript_score[d.record.id]), d.record.id))
    count = min(max(0, deep_papers), len(initial))
    top_ids = {d.record.id for d in initial[:count]}
    outgoing = {d.record.id: {citation for p in d.passages for citation in p.citations}
                for d in papers}
    linked = {d.record.id: float(len((outgoing[d.record.id] & top_ids) |
              {source for source in top_ids if d.record.id in outgoing[source]})) for d in papers}
    rows = []
    for doc in papers:
        pid = doc.record.id
        links = linked[pid]
        total = lexical[pid] + manuscript_score[pid] + links
        rows.append(Relevance(pid, lexical[pid], citation_count.get(pid, 0),
                              len(citation_sections.get(pid, ())), links, total, 'abstract'))
    ordered = sorted(rows, key=lambda r: (-r.total, r.paper_id))
    reserve = count // 10
    base_scores = {row.paper_id: row.total - row.citation_links for row in rows}
    base_order = sorted(rows, key=lambda r: (-base_scores[r.paper_id], r.paper_id))
    lower_ids = {r.paper_id for r in base_order[count:]}
    explore = [r for r in ordered if r.paper_id in lower_ids and r.citation_links > 0][:reserve]
    chosen = {r.paper_id for r in base_order[:count-len(explore)]} | {r.paper_id for r in explore}
    for row in ordered:
        if len(chosen) >= count:
            break
        chosen.add(row.paper_id)
    return tuple(replace(r, tier='deep' if r.paper_id in chosen else 'abstract') for r in ordered)


def select_sections(document, limit=8, *, section_names=None):
    wanted = section_names or ('abstract', 'introduction', 'limitations', 'limitation', 'discussion',
                               'conclusion', 'conclusions', 'future work', 'future directions')
    result = []
    for passage in document.passages:
        section = re.sub(r'^\s*\d+(?:\.\d+)*\.?\s*', '', passage.section.casefold()).strip()
        if any(name in section for name in wanted):
            result.append(passage)
    return tuple(result[:limit])


def parse_fragment(raw, documents):
    """Validate nodes and edge endpoints; silently discard bad-quote edges with an audit note."""
    object_fields(raw, ('nodes', 'edges'))
    paper_ids = {d.record.id for d in documents if d.origin == 'paper'}
    nodes, seen = [], set()
    for item in array(raw['nodes']):
        object_fields(item, ('id', 'type', 'name'), ('paper_id',))
        if item['type'] not in NODE_TYPES:
            raise ValidationError('unknown graph node type')
        node_id, name = text(item['id']), text(item['name'])
        if node_id in seen:
            raise ValidationError('duplicate graph node id')
        seen.add(node_id)
        if item['type'] == 'paper' and node_id not in paper_ids:
            raise ValidationError('paper graph node must identify a corpus paper')
        nodes.append(GraphNode(node_id, item['type'], name, (), item.get('paper_id', '')))
    edges, dropped = [], []
    by_id = {node.id: node for node in nodes}
    endpoints = {'addresses': ('method', 'problem'), 'assumes': ('method', 'assumption'),
        'fails_under': ('method', 'failure_condition'), 'evaluated_on': ('method', 'setting'),
        'extends': ('method', 'method'), 'compares': ('method', 'method'), 'reports': ('paper', 'finding'),
        'contradicts': ('finding', 'finding')}
    for item in array(raw['edges']):
        object_fields(item, ('id', 'type', 'source', 'target', 'source_paper', 'evidence'))
        if item['type'] not in EDGE_TYPES:
            raise ValidationError('unknown graph edge type')
        if item['source'] not in seen or item['target'] not in seen:
            raise ValidationError('graph edge references unknown node')
        if item['type'] == 'handles':
            valid = by_id[item['source']].type == 'method' and by_id[item['target']].type in ('failure_condition', 'assumption')
        else:
            source_type, target_type = endpoints[item['type']]
            valid = by_id[item['source']].type == source_type and by_id[item['target']].type == target_type
        if not valid:
            raise ValidationError('graph edge endpoints do not match edge type')
        try:
            spans = tuple(evidence(e, documents) for e in array(item['evidence']))
            if not spans:
                raise UnsupportedEvidence('edge has no evidence')
        except UnsupportedEvidence:
            dropped.append('dropped unsupported edge ' + text(item['id']))
            continue
        if item['source_paper'] not in paper_ids:
            dropped.append('dropped edge with unknown source paper ' + text(item['id']))
            continue
        edges.append(GraphEdge(text(item['id']), item['type'], item['source'], item['target'],
                               item['source_paper'], spans))
    return ResearchGraph(tuple(nodes), tuple(edges), tuple(dropped))


def namespace_fragment(graph, namespace):
    """Make call-local role ids globally unique while preserving corpus paper ids."""
    mapping = {node.id: node.id if node.type == 'paper' else f'{namespace}__{node.id}'
               for node in graph.nodes}
    nodes = tuple(replace(node, id=mapping[node.id]) for node in graph.nodes)
    edges = tuple(replace(edge, id=f'{namespace}__{edge.id}',
                          source=mapping[edge.source], target=mapping[edge.target]) for edge in graph.edges)
    return ResearchGraph(nodes, edges, graph.dropped)


def canonicalize(graph, threshold=0.86):
    """Merge near-duplicate names of one type while retaining spelling aliases."""
    nodes, redirects, aliases = [], {}, {}
    for node in graph.nodes:
        normalized = ' '.join(_tokens(node.name))
        match = next((old for old in nodes if old.type == node.type and
                      SequenceMatcher(None, normalized, ' '.join(_tokens(old.name))).ratio() >= threshold), None)
        if match is None:
            nodes.append(node); redirects[node.id] = node.id; aliases[node.id] = list(node.aliases)
        else:
            redirects[node.id] = match.id
            aliases[match.id].extend([node.name, *node.aliases])
    canonical = tuple(replace(n, aliases=tuple(dict.fromkeys(aliases[n.id]))) for n in nodes)
    edges = tuple(replace(e, source=redirects[e.source], target=redirects[e.target]) for e in graph.edges
                  if redirects[e.source] != redirects[e.target])
    return ResearchGraph(canonical, edges, graph.dropped)


def detect_gaps(graph, *, topic_settings=(), min_support=2):
    """Find structural patterns and return the graph identifiers supporting each candidate."""
    nodes = {n.id: n for n in graph.nodes}
    grouped = {kind: [n for n in graph.nodes if n.type == kind] for kind in NODE_TYPES}
    edges, result = graph.edges, []
    def add(pattern, statement, node_ids, edge_ids):
        result.append(GapCandidate(pattern, statement, tuple(sorted(set(node_ids))), tuple(sorted(set(edge_ids)))))
    handled = {e.target for e in edges if e.type == 'handles'}
    for assumption in grouped['assumption']:
        for problem in grouped['problem']:
            addressing = {e.source for e in edges if e.type == 'addresses' and e.target == problem.id}
            supporting = [e for e in edges if e.type == 'assumes' and e.target == assumption.id and e.source in addressing]
            if len({e.source for e in supporting}) >= min_support and assumption.id not in handled:
                add('shared_assumption', f'Methods addressing {problem.name} share {assumption.name}.',
                    [problem.id, assumption.id, *(e.source for e in supporting)], [e.id for e in supporting])
    for failure in grouped['failure_condition']:
        supporting = [e for e in edges if e.type == 'fails_under' and e.target == failure.id]
        if len({e.source for e in supporting}) >= min_support and failure.id not in handled:
            add('recurring_failure', f'Multiple methods fail under {failure.name}.',
                [failure.id, *(e.source for e in supporting)], [e.id for e in supporting])
    for problem in grouped['problem']:
        addressing = [e for e in edges if e.type == 'addresses' and e.target == problem.id]
        method_ids = {e.source for e in addressing}
        for setting in grouped['setting']:
            evaluated = [e for e in edges if e.type == 'evaluated_on' and e.target == setting.id]
            if len(method_ids) >= min_support and len({e.source for e in evaluated}) >= min_support and not method_ids & {e.source for e in evaluated}:
                support = [*addressing, *evaluated]
                add('uncovered_combination', f'No method for {problem.name} is evaluated on {setting.name}.',
                    [problem.id, setting.id, *method_ids, *(e.source for e in evaluated)], [e.id for e in support])
            if topic_settings and any(term in setting.name.casefold() for term in topic_settings) and method_ids and not any(
                    e.type == 'evaluated_on' and e.source in method_ids and e.target == setting.id for e in edges):
                add('evaluation_gap', f'Methods for {problem.name} have no evaluation on {setting.name}.',
                    [problem.id, setting.id, *method_ids], [e.id for e in addressing])
    for edge in edges:
        if edge.type == 'contradicts' and edge.source in nodes and edge.target in nodes:
            add('contradiction', f'Findings contradict: {nodes[edge.source].name} / {nodes[edge.target].name}.',
                [edge.source, edge.target], [edge.id])
    positive_terms = ('improv', 'increase', 'outperform', 'effective', 'benefit', 'success', 'better')
    negative_terms = ('worsen', 'decreas', 'fail', 'unstable', 'no effect', 'detrimental', 'worse')
    reports = [e for e in edges if e.type == 'reports' and e.source in nodes and e.target in nodes]
    for left_index, left in enumerate(reports):
        left_name = nodes[left.target].name.casefold()
        left_sign = 1 if any(term in left_name for term in positive_terms) else -1 if any(term in left_name for term in negative_terms) else 0
        if not left_sign: continue
        for right in reports[left_index+1:]:
            right_name = nodes[right.target].name.casefold()
            right_sign = 1 if any(term in right_name for term in positive_terms) else -1 if any(term in right_name for term in negative_terms) else 0
            if left_sign == right_sign or not right_sign: continue
            left_methods = {e.source for e in edges if e.source_paper == left.source and e.type in ('addresses', 'evaluated_on')}
            right_methods = {e.source for e in edges if e.source_paper == right.source and e.type in ('addresses', 'evaluated_on')}
            shared_methods = left_methods & right_methods
            for method in shared_methods:
                left_settings = {e.target for e in edges if e.type == 'evaluated_on' and e.source == method and e.source_paper == left.source}
                right_settings = {e.target for e in edges if e.type == 'evaluated_on' and e.source == method and e.source_paper == right.source}
                for setting in left_settings & right_settings:
                    add('contradiction', f'Opposite findings for {nodes[method].name} on {nodes[setting].name}.',
                        [left.target, right.target, method, setting], [left.id, right.id])
    for problem in grouped['problem']:
        methods = [e for e in edges if e.type == 'addresses' and e.target == problem.id]
        for index, left in enumerate(methods):
            for right in methods[index+1:]:
                related = any(e.type in ('extends', 'compares') and {e.source, e.target} == {left.source, right.source} for e in edges)
                if left.source != right.source and not related:
                    add('missing_bridge', f'Methods for {problem.name} lack a comparison or extension link.',
                        [problem.id, left.source, right.source], [left.id, right.id])
    return tuple(result)
