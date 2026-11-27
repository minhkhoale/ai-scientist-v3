import pytest
from src.research_discovery.distance import (FACETS, candidate_methods, gap_distance, graph_frontier_distance,
                                             graph_priors, parse_coverage, semantic_distance)
from src.research_discovery.graph import GraphEdge, GraphNode, ResearchGraph
from src.research_discovery.ingest import make_record, passages
from src.research_discovery.models import Document, Evidence, ValidationError
from src.research_discovery.verification import SearchAudit, Verification


def doc(title, body):
    record = make_record({'title': title, 'abstract': body, 'url': 'https://example.org/' + title.replace(' ', '-')})
    return Document(record, 'paper', passages(body, record.id))


class Gap:
    def __init__(self, nodes):
        self.id, self.statement, self.graph_node_ids, self.closest_prior = 'g', 'critic bias in diffusion credit', nodes, ('L1', 'L2')


def setup():
    paper = doc('Critic paper', 'The method assumes an unbiased critic for denoising credit.')
    span = Evidence(paper.record.id, paper.passages[0].id, 0, 10, paper.passages[0].text[:10])
    nodes = (GraphNode('p', 'problem', 'Credit'), GraphNode('a', 'assumption', 'Unbiased critic'),
             GraphNode('m1', 'method', 'One'), GraphNode('m2', 'method', 'Two'))
    edges = (GraphEdge('e1', 'addresses', 'm1', 'p', paper.record.id, (span,)),
             GraphEdge('e2', 'addresses', 'm2', 'p', paper.record.id, (span,)),
             GraphEdge('e3', 'assumes', 'm1', 'a', paper.record.id, (span,)))
    return paper, span, ResearchGraph(nodes, edges, ()), Gap(('p', 'a'))


def quote(paper):
    p = paper.passages[0]
    return {'source': paper.record.id, 'passage': p.id, 'start': 0, 'end': 10, 'quote': p.text[:10]}


def cells(**levels):
    return {f: {'level': levels.get(f, 'unknown'), 'evidence': levels.get(f + '_ev', [])} for f in FACETS}


def test_coverage_needs_quotes_and_keeps_unknown_as_an_interval():
    paper, _, graph, gap = setup()
    methods = candidate_methods(gap, graph)
    priors = graph_priors(gap, graph, methods)
    assert priors['m1']['failure'].level == 'not_covered' and priors['m1']['problem'].level == 'covered'
    raw = {'facets': {f: f for f in FACETS}, 'methods': [
        {'method_id': 'm1', 'cells': cells(resolution='covered'), 'required_changes': [{'kind': 'relax_assumption', 'description': 'x'}]},
        {'method_id': 'm2', 'cells': cells(setting='not_covered'), 'required_changes': []},
        {'method_id': 'ghost', 'cells': cells(), 'required_changes': []}]}
    _, coverage = parse_coverage(raw, methods=methods, priors=priors, documents=[paper])
    by_id = {m.method_id: m for m in coverage}
    # Unquoted "covered" and unsupported "not_covered" become unknown; graph priors fill the gaps.
    assert by_id['m1'].cells['resolution'].level == 'unknown'
    assert by_id['m1'].cells['failure'].level == 'not_covered' and by_id['m1'].cells['problem'].source == 'graph'
    assert by_id['m2'].cells['setting'].level == 'unknown'
    assert (by_id['m1'].coverage_low, by_id['m1'].coverage_high) == (0.2, 0.7)
    with pytest.raises(ValidationError):
        parse_coverage({**raw, 'methods': [{'method_id': 'm1', 'cells': cells(problem='maybe'), 'required_changes': []}]},
                       methods=methods, priors=priors, documents=[paper])


def test_scd_interval_resolving_method_and_confidence():
    paper, _, graph, gap = setup()
    methods = candidate_methods(gap, graph)
    priors = graph_priors(gap, graph, methods)
    ev = [quote(paper)]
    raw = {'facets': {f: f for f in FACETS}, 'methods': [
        {'method_id': 'm2', 'cells': cells(problem='covered', problem_ev=ev, setting='covered', setting_ev=ev,
                                           failure='covered', failure_ev=ev, resolution='covered', resolution_ev=ev),
         'required_changes': []}]}
    facets, coverage = parse_coverage(raw, methods=methods, priors=priors, documents=[paper])
    verification = Verification('g', 'partially_addressed', 'r', (), (), (), (SearchAudit('q', 'ok', ()), SearchAudit('q2', 'failed', ())))
    d = gap_distance(gap, graph, verification, [paper], coverage, facets)
    assert (d.scd_low, d.scd_high) == (0.0, 0.0) and d.resolving_method == 'm2' and d.closest_method == 'm2'
    assert d.confidence['search_coverage'] == 0.5 and 0 < d.confidence['known_cell_fraction'] < 1


def test_frontier_path_prefers_cheap_typed_edges_and_semantic_distance_finds_neighbor():
    paper, _, graph, gap = setup()
    assert graph_frontier_distance(gap, graph, ['m1']) == 1.0
    assert graph_frontier_distance(gap, graph, ['m2']) == 3.0
    assert graph_frontier_distance(Gap(()), graph, ['m1']) is None
    other = doc('Unrelated', 'protein folding with language models')
    snd, nearest = semantic_distance('unbiased critic denoising credit', [paper, other])
    assert nearest == paper.record.id and 0 <= snd < 1


def test_scd_without_prior_methods_is_unknown_not_maximal_distance():
    paper, _, graph, gap = setup()
    verification = Verification('g', 'uncertain', 'No relevant method found.', (), (), (), ())
    distance = gap_distance(gap, graph, verification, [paper], (), {f: f for f in FACETS})
    assert (distance.scd_low, distance.scd_high) == (0.0, 1.0)
    assert distance.closest_method == ''
    assert distance.confidence['known_cell_fraction'] == 0.0
