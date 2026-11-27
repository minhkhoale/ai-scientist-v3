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


def test_manuscript_fragment_keeps_cited_paper_nodes_and_drops_others():
    review = doc('Review', 'Cited work finds a result.', 'manuscript', ['Lcited'])
    rid, passage = review.record.id, review.passages[0]
    quote = {'source': rid, 'passage': passage.id, 'start': 0, 'end': 10, 'quote': passage.text[:10]}
    raw = {'nodes': [{'id': 'Lcited', 'type': 'paper', 'name': 'Cited'},
                     {'id': 'Lother', 'type': 'paper', 'name': 'Other'},
                     {'id': 'f', 'type': 'finding', 'name': 'Result'}],
           'edges': [{'id': 'e1', 'type': 'reports', 'source': 'Lcited', 'target': 'f', 'source_paper': rid, 'evidence': [quote]},
                     {'id': 'e2', 'type': 'reports', 'source': 'Lother', 'target': 'f', 'source_paper': rid, 'evidence': [quote]}]}
    result = parse_fragment(raw, [review])
    assert [n.id for n in result.nodes] == ['Lcited', 'f'] and [e.id for e in result.edges] == ['e1']
    assert len(result.dropped) == 2


def test_merging_steps_keeps_one_paper_node_with_alternate_titles():
    from src.research_discovery.graph_pipeline import _merge_graphs
    a = ResearchGraph((GraphNode('L1', 'paper', 'DIME', (), ''),), (), ())
    b = ResearchGraph((GraphNode('L1', 'paper', 'DIME: diffusion max-ent RL', (), ''),), (), ())
    merged = _merge_graphs([a, b])
    assert [(n.id, n.name, n.aliases) for n in merged.nodes] == [('L1', 'DIME', ('DIME: diffusion max-ent RL',))]


def test_shortlist_caps_each_pattern_and_prefers_multi_paper_support():
    from src.research_discovery.graph import GapCandidate
    from src.research_discovery.graph_pipeline import _shortlist
    edges = tuple(GraphEdge(f'e{i}', 'addresses', 'm', 'p', f'L{i}', ()) for i in range(3))
    graph = ResearchGraph((), edges, ())
    weak = [GapCandidate('evaluation_gap', f'weak {i}', ('n',), ('e0',)) for i in range(5)]
    strong = GapCandidate('evaluation_gap', 'strong', ('n',), ('e0', 'e1', 'e2'))
    other = GapCandidate('contradiction', 'other', ('n',), ('e1',))
    kept = _shortlist([*weak, strong, other], graph, 2, 10)
    assert kept[0].statement == 'strong' and len([c for c in kept if c.pattern == 'evaluation_gap']) == 2
    assert other in kept


def test_semantic_canonicalization_connects_nodes_from_different_steps(tmp_path):
    import json
    from src.research_discovery.artifacts import Steps
    from src.research_discovery.graph_pipeline import _confirm_canonicalization
    from src.ideation.role_runner import RoleAgent
    def role(name, prompt, **kw):
        data = json.loads(prompt.split('UNTRUSTED DATA\n', 1)[1])
        ids = [i for c in data['clusters'] for i in c['node_ids']]
        return {'clusters': [{'node_ids': ids, 'canonical': ids[0], 'name': 'Biased critic'}]}
    nodes = (GraphNode('s1__a', 'assumption', 'Biased critic'), GraphNode('s2__a', 'assumption', 'Q-value overestimation'),
             GraphNode('m1', 'method', 'One'), GraphNode('m2', 'method', 'Two'))
    edges = (GraphEdge('e1', 'assumes', 'm1', 's1__a', 'L1', ()), GraphEdge('e2', 'assumes', 'm2', 's2__a', 'L2', ()))
    graph = _confirm_canonicalization(ResearchGraph(nodes, edges, ()), Steps(tmp_path, RoleAgent('codex'), role), RoleAgent('codex'))
    assert {e.target for e in graph.edges} == {'s1__a'}
    assert next(n for n in graph.nodes if n.id == 's1__a').aliases == ('Q-value overestimation',)


def test_targeted_fragment_links_new_nodes_to_known_graph_nodes():
    source = doc('Source', 'Exact supporting statement.')
    paper, passage = source.record.id, source.passages[0]
    known = GraphNode('step__p', 'problem', 'Problem')
    span = {'source': paper, 'passage': passage.id, 'start': 0, 'end': 5, 'quote': passage.text[:5]}
    raw = {'nodes': [{'id': 'step__p', 'type': 'problem', 'name': 'Problem'}, {'id': 'n1', 'type': 'method', 'name': 'M'}],
           'edges': [{'id': 'e1', 'type': 'addresses', 'source': 'n1', 'target': 'step__p', 'source_paper': paper, 'evidence': [span]}]}
    fragment = namespace_fragment(parse_fragment(raw, [source], [known]), 'targeted')
    assert [n.id for n in fragment.nodes] == ['targeted__n1']
    assert [(e.source, e.target) for e in fragment.edges] == [('targeted__n1', 'step__p')]


def test_synthesized_gaps_need_a_problem_and_cite_supporting_edges():
    import pytest
    from src.research_discovery.graph_pipeline import _synthesis_view, _validate_synthesis
    from src.research_discovery.models import ValidationError
    nodes = (GraphNode('p', 'problem', 'Credit'), GraphNode('m1', 'method', 'One'), GraphNode('m2', 'method', 'Two'),
             GraphNode('a', 'assumption', 'Unbiased critic'))
    edges = (GraphEdge('e1', 'addresses', 'm1', 'p', 'L1', ()), GraphEdge('e2', 'addresses', 'm2', 'p', 'L2', ()),
             GraphEdge('e3', 'assumes', 'm1', 'a', 'L1', ()), GraphEdge('e4', 'assumes', 'm2', 'a', 'L2', ()))
    graph = ResearchGraph(nodes, edges, ())
    view = _synthesis_view(graph)
    assert view[0]['problem']['id'] == 'p' and {a['id'] for a in view[0]['assumptions']} == {'a'}
    row = {'kind': 'shared_assumption', 'statement': 'All assume an unbiased critic.', 'rationale': 'Two papers.'}
    [gap] = _validate_synthesis({'gaps': [{**row, 'node_ids': ['p', 'm1', 'm2', 'a', 'ghost']},
                                          {**row, 'node_ids': ['m1', 'a']}]}, graph)
    assert gap.pattern == 'synthesis:shared_assumption' and set(gap.edge_ids) == {'e1', 'e2', 'e3', 'e4'}
    with pytest.raises(ValidationError):
        _validate_synthesis({'gaps': [{**row, 'kind': 'vibes', 'node_ids': ['p']}]}, graph)


def test_prompt_data_trims_documents_and_drops_duplicate_abstracts():
    import json
    from dataclasses import asdict
    from src.research_discovery.models import prompt_data
    paper = doc('Paper', 'An abstract sentence that a passage repeats.')
    review = doc('Review', 'Cites the paper.', 'manuscript', ['Lcited'])
    data = json.loads(prompt_data({'topic': 't', 'documents': [asdict(paper), asdict(review)]}))
    first, second = data['documents']
    assert set(first['record']) == {'id', 'title'} and first['origin'] == 'paper'
    assert set(first['passages'][0]) == {'id', 'section', 'text'}
    assert second['passages'][0]['citations'] == ['Lcited']
    assert ': ' not in prompt_data({'a': 1, 'b': [1, 2]})


def test_manuscript_is_extracted_in_batches_of_24_passages():
    from src.research_discovery.config import DiscoverySettings
    from src.research_discovery.graph_pipeline import estimate_call_count
    settings = DiscoverySettings()
    assert estimate_call_count(0, 122, settings) - estimate_call_count(0, 0, settings) == 6
