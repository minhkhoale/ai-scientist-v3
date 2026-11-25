from src.research_discovery.graph import (GraphEdge, GraphNode, ResearchGraph,
    canonicalize, detect_gaps, namespace_fragment, parse_fragment, rank_relevance, select_sections)
from src.research_discovery.ingest import make_record, passages
from src.research_discovery.models import Document


def doc(title, body, origin='paper', citations=()):
    record = make_record({'title': title, 'abstract': body, 'url': 'https://example.org/' + title.replace(' ', '-')})
    ps = passages(body, record.id)
    if citations and ps:
        from dataclasses import replace
        ps = (replace(ps[0], citations=tuple(citations)), *ps[1:])
    return Document(record, origin, tuple(ps))


def test_relevance_reserves_linked_exploration_slot():
    papers = [doc('Feedback diffusion study', 'feedback diffusion control')]
    papers.extend(doc(f'Unrelated policy {i}', 'miscellaneous controls') for i in range(19))
    linked_id = papers[-1].record.id
    from dataclasses import replace
    papers[-1] = replace(papers[-1], passages=(replace(papers[-1].passages[0], citations=(papers[0].record.id,)),))
    review = doc('Review', 'feedback topic', 'manuscript', [papers[0].record.id])
    ranking = rank_relevance('feedback diffusion', [*papers, review], deep_papers=10)
    tiers = {item.paper_id: item.tier for item in ranking}
    assert sum(tier == 'deep' for tier in tiers.values()) == 10
    assert tiers[linked_id] == 'deep'
    assert next(r for r in ranking if r.paper_id == linked_id).citation_links > 0
    assert next(r for r in ranking if r.paper_id == papers[0].record.id).manuscript_sections == 1


def test_select_sections_uses_names_and_caps_passages():
    paper = doc('Sections', '# Abstract\n\nabstract text\n\n## Introduction\n\nintro\n\n## Methods\n\nmethod\n\n## Limitations and Future Work\n\nlimit')
    selected = select_sections(paper, limit=2)
    assert [p.section for p in selected] == ['Abstract', 'Introduction']
    assert all('method' not in p.text for p in selected)


def test_invalid_edge_quote_is_dropped():
    source = doc('Source', 'Exact supporting statement.')
    paper, passage = source.record.id, source.passages[0]
    raw = {'nodes': [{'id': 'm', 'type': 'method', 'name': 'Method'},
                     {'id': 'p', 'type': 'problem', 'name': 'Problem'}],
           'edges': [{'id': 'e', 'type': 'addresses', 'source': 'm', 'target': 'p', 'source_paper': paper,
                      'evidence': [{'source': paper, 'passage': passage.id, 'start': 0, 'end': 5, 'quote': 'wrong'}]}]}
    result = parse_fragment(raw, [source])
    assert result.edges == ()
    assert result.dropped == ('dropped unsupported edge e',)


def test_canonicalize_merges_same_type_near_duplicates_only():
    graph = ResearchGraph((GraphNode('a', 'method', 'Diffusion policy'),
                           GraphNode('b', 'method', 'Diffusion policies'),
                           GraphNode('c', 'problem', 'Diffusion policies')), ())
    result = canonicalize(graph)
    assert [n.id for n in result.nodes] == ['a', 'c']
    assert 'Diffusion policies' in result.nodes[0].aliases


def test_fragment_namespaces_call_local_ids_but_preserves_paper_ids():
    source = doc('Source', 'Exact supporting statement.')
    passage = source.passages[0]
    raw = {'nodes': [{'id': source.record.id, 'type': 'paper', 'name': 'Source'},
                     {'id': 'n1', 'type': 'finding', 'name': 'Finding'}],
           'edges': [{'id': 'e1', 'type': 'reports', 'source': source.record.id, 'target': 'n1',
             'source_paper': source.record.id, 'evidence': [{'source': source.record.id,
             'passage': passage.id, 'start': 0, 'end': len(passage.text), 'quote': passage.text}]}]}
    fragment = namespace_fragment(parse_fragment(raw, [source]), 'batch_1')
    assert [node.id for node in fragment.nodes] == [source.record.id, 'batch_1__n1']
    assert fragment.edges[0].id == 'batch_1__e1'
    assert fragment.edges[0].target == 'batch_1__n1'


def test_each_gap_detector_has_positive_and_negative_cases():
    nodes = tuple(GraphNode(i, kind, name) for i, kind, name in [
        ('p', 'problem', 'control'), ('m1', 'method', 'A'), ('m2', 'method', 'B'),
        ('a', 'assumption', 'known state'), ('f', 'failure_condition', 'long horizon'),
        ('s', 'setting', 'simulation'), ('s2', 'setting', 'field'),
        ('m3', 'method', 'C'), ('m4', 'method', 'D')])
    def edge(i, kind, src, dst):
        return GraphEdge(i, kind, src, dst, 'paper', ())
    edges = (edge('a1', 'addresses', 'm1', 'p'), edge('a2', 'addresses', 'm2', 'p'),
             edge('h1', 'assumes', 'm1', 'a'), edge('h2', 'assumes', 'm2', 'a'),
             edge('f1', 'fails_under', 'm1', 'f'), edge('f2', 'fails_under', 'm2', 'f'),
             edge('e1', 'evaluated_on', 'm1', 's'), edge('e2', 'evaluated_on', 'm2', 's'),
             edge('e3', 'evaluated_on', 'm3', 's2'), edge('e4', 'evaluated_on', 'm4', 's2'),
             edge('c', 'contradicts', 'x', 'y'))
    # Contradiction fixture gets its own valid finding nodes and edge.
    contradiction = ResearchGraph((*nodes, GraphNode('x', 'finding', 'improves'),
        GraphNode('y', 'finding', 'degrades')), (*edges[:-1], edge('c', 'contradicts', 'x', 'y')))
    found = {gap.pattern for gap in detect_gaps(contradiction, topic_settings=('field',), min_support=2)}
    assert {'shared_assumption', 'recurring_failure', 'uncovered_combination',
            'contradiction', 'missing_bridge', 'evaluation_gap'} <= found
    negative = ResearchGraph(nodes, tuple(e for e in edges[:-1] if e.id not in ('h2', 'f2')))
    patterns = {gap.pattern for gap in detect_gaps(negative, topic_settings=('absent',), min_support=2)}
    assert 'shared_assumption' not in patterns and 'recurring_failure' not in patterns
    assert 'contradiction' not in patterns and 'evaluation_gap' not in patterns


def test_handled_assumption_and_failure_do_not_produce_candidates():
    nodes = (GraphNode('p', 'problem', 'problem'), GraphNode('m1', 'method', 'one'),
             GraphNode('m2', 'method', 'two'), GraphNode('a', 'assumption', 'assumption'),
             GraphNode('f', 'failure_condition', 'failure'))
    edges = tuple(GraphEdge(i, kind, src, dst, 'paper', ()) for i, kind, src, dst in [
        ('a1', 'addresses', 'm1', 'p'), ('a2', 'addresses', 'm2', 'p'),
        ('s1', 'assumes', 'm1', 'a'), ('s2', 'assumes', 'm2', 'a'),
        ('f1', 'fails_under', 'm1', 'f'), ('f2', 'fails_under', 'm2', 'f'),
        ('h1', 'handles', 'm1', 'a'), ('h2', 'handles', 'm2', 'f')])
    patterns = {gap.pattern for gap in detect_gaps(ResearchGraph(nodes, edges), min_support=2)}
    assert 'shared_assumption' not in patterns and 'recurring_failure' not in patterns


def test_uncovered_combination_and_missing_bridge_are_suppressed_when_covered_or_linked():
    nodes = (GraphNode('p', 'problem', 'control'), GraphNode('m1', 'method', 'one'),
             GraphNode('m2', 'method', 'two'), GraphNode('s', 'setting', 'field'))
    def edge(i, kind, src, dst): return GraphEdge(i, kind, src, dst, 'paper', ())
    edges = (edge('a1', 'addresses', 'm1', 'p'), edge('a2', 'addresses', 'm2', 'p'),
             edge('e1', 'evaluated_on', 'm1', 's'), edge('e2', 'evaluated_on', 'm2', 's'),
             edge('x', 'extends', 'm1', 'm2'))
    patterns = {gap.pattern for gap in detect_gaps(ResearchGraph(nodes, edges), min_support=2)}
    assert 'uncovered_combination' not in patterns and 'missing_bridge' not in patterns


def test_opposite_reported_results_on_same_method_and_setting_are_contradictory():
    nodes = (GraphNode('m', 'method', 'Controller'), GraphNode('s', 'setting', 'field'),
             GraphNode('f1', 'finding', 'performance improves'), GraphNode('f2', 'finding', 'performance worsens'),
             GraphNode('p1', 'paper', 'Paper 1'), GraphNode('p2', 'paper', 'Paper 2'))
    edges = tuple(GraphEdge(i, kind, src, dst, paper, ()) for i, kind, src, dst, paper in [
        ('r1', 'reports', 'p1', 'f1', 'p1'), ('r2', 'reports', 'p2', 'f2', 'p2'),
        ('e1', 'evaluated_on', 'm', 's', 'p1'), ('e2', 'evaluated_on', 'm', 's', 'p2')])
    result = detect_gaps(ResearchGraph(nodes, edges))
    assert any(g.pattern == 'contradiction' and {'f1', 'f2', 'm', 's'} <= set(g.node_ids) for g in result)
    no_shared_setting = tuple(edge for edge in edges if edge.id != 'e2')
    assert not any(g.pattern == 'contradiction' and 'f1' in g.node_ids and 'f2' in g.node_ids
                   for g in detect_gaps(ResearchGraph(nodes, no_shared_setting)))
