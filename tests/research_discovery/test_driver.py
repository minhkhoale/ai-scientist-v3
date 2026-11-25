import json
import pytest

from src.research_discovery.config import DiscoverySettings
from src.research_discovery.driver import run_discovery
from src.research_discovery.graph import GraphEdge, GraphNode, ResearchGraph, parse_fragment
from src.research_discovery.graph_pipeline import integrate_targeted_read
from src.ideation.role_runner import RoleAgent


def setup(tmp_path):
    papers = tmp_path / 'papers'; papers.mkdir()
    for i in range(3):
        (papers / f'{i}.md').write_text(
            f'# Paper {i}\n\n## Abstract\n\nA feedback method for long rollouts, paper {i}.\n\n'
            f'## Introduction\n\nFeedback remains unstable in long rollouts for method {i}.\n\n'
            '## Methods\n\nWe evaluate a feedback controller.')
    review = tmp_path / 'review.md'; review.write_text('# Review\n\nFeedback policies need long horizon evaluation.')
    class Provider:
        def __init__(self): self.searches = []
        def search(self, query, limit): self.searches.append(query); return []
        def expand(self, paper, limit): return []
        def fulltext(self, paper, out): return ''
    return papers, review, Provider()


def fake_roles(calls, *, kill=None):
    def role(name, prompt, **kwargs):
        if kill == name: raise RuntimeError('interrupted')
        calls.append(name)
        data = json.loads(prompt.split('\nUNTRUSTED DATA\n', 1)[1])
        if name in ('graph_extract', 'targeted_read'):
            if name == 'targeted_read': return {'nodes': [], 'edges': []}
            nodes, edges = [], []
            for document in data['documents']:
                if document['origin'] != 'paper' or not document['passages']: continue
                source = document['record']['id']; passage = document['passages'][0]
                nodes.extend([{'id': 'problem_' + source, 'type': 'problem', 'name': 'Long horizon control'},
                              {'id': 'assumption_' + source, 'type': 'assumption', 'name': 'Feedback is observable'},
                              {'id': 'method_' + source, 'type': 'method', 'name': 'Feedback method ' + source}])
                span = {'source': source, 'passage': passage['id'], 'start': 0,
                        'end': len(passage['text']), 'quote': passage['text']}
                edges.extend([{'id': 'addresses_' + source, 'type': 'addresses', 'source': 'method_' + source,
                    'target': 'problem_' + source, 'source_paper': source, 'evidence': [span]},
                    {'id': 'assumes_' + source, 'type': 'assumes', 'source': 'method_' + source,
                    'target': 'assumption_' + source, 'source_paper': source, 'evidence': [span]}])
            return {'nodes': nodes, 'edges': edges}
        if name == 'canonicalize':
            return {'clusters': data['clusters']}
        if name == 'synthesize':
            problem = next(n['id'] for n in (row['problem'] for row in data['roadmap']))
            return {'gaps': [{'kind': 'shared_assumption', 'statement': 'Synthesized gap.', 'rationale': 'Several papers.',
                              'node_ids': [problem, 'not_a_node']}]}
        if name == 'screen':
            return {'decisions': [{'candidate': i, 'rank': i + 1, 'accepted': True, 'reason': 'Supported pattern.'}
                                  for i, _ in enumerate(data['candidates'])]}
        if name == 'verify':
            doc = next(d for d in data['documents'] if d['origin'] == 'paper')
            passage = doc['passages'][0]
            evidence = {'source': doc['record']['id'], 'passage': passage['id'], 'start': 0,
                        'end': len(passage['text']), 'quote': passage['text']}
            return {'verdict': 'open', 'rationale': 'The corpus reports the pattern.',
                'positive_evidence': [evidence], 'counter_evidence': [], 'resolving_sources': []}
        if name == 'assess':
            doc = next(d for d in data['documents'] if d['origin'] == 'paper')
            passage = doc['passages'][0]
            evidence = {'source': doc['record']['id'], 'passage': passage['id'], 'start': 0,
                        'end': len(passage['text']), 'quote': passage['text']}
            return {**{axis: {'score': 3, 'rationale': 'Supported by the source.', 'evidence': [evidence]}
                       for axis in ('importance', 'difficulty', 'impact', 'feasibility')},
                    'uncertainty': 'The corpus is bounded.'}
        raise AssertionError(name)
    return role


def args(tmp_path, run_role_fn):
    papers, review, provider = setup(tmp_path)
    return dict(topic='Feedback long horizon policies', papers=[papers], manuscript=review,
        out_dir=tmp_path / 'out', generator=RoleAgent('codex'), judge=RoleAgent('claude'),
        run_role_fn=run_role_fn, provider=provider,
        settings=DiscoverySettings(deep_papers=2, expansion_limit=0), log=lambda *_: None)


def test_graph_pipeline_writes_tiered_artifacts_and_handoff(tmp_path):
    calls = []; kw = args(tmp_path, fake_roles(calls)); report = run_discovery(**kw)
    out = kw['out_dir']
    assert report.exists() and (out / 'roadmap.md').exists()
    graph = json.loads((out / 'graph.json').read_text())
    assert any(n['tier'] == 'deep' for n in graph['nodes'] if n['type'] == 'paper')
    rows = json.loads((out / 'gaps.json').read_text())['gaps']
    assert rows and all(row['pattern'] and row['family'] for row in rows)
    from src.ideation.proposal.schema import parse_gaps
    from src.ideation.literature.corpus import load_corpus
    allowed = {record.id for record in load_corpus(out / 'corpus')}
    assert len(parse_gaps({'gaps': rows}, allowed)) >= 3
    assert 'Estimated LLM calls before extraction' not in report.read_text()
    assert json.loads((out / 'status.json').read_text())['estimated_calls'] > 0
    assert 'graph_extract' in calls and 'screen' in calls and 'canonicalize' in calls


def test_graph_pipeline_resumes_after_interruption_without_repeating_finished_steps(tmp_path):
    calls = []; kw = args(tmp_path, fake_roles(calls, kill='screen'))
    with pytest.raises(RuntimeError, match='interrupted'):
        run_discovery(**kw)
    before = list(calls)
    run_discovery(**{**kw, 'run_role_fn': fake_roles(calls)})
    assert calls[:len(before)] == before
    assert calls.count('canonicalize') == before.count('canonicalize')


def test_targeted_read_can_remove_a_structural_gap():
    nodes = (GraphNode('p', 'problem', 'control'), GraphNode('m1', 'method', 'one'),
             GraphNode('m2', 'method', 'two'), GraphNode('a', 'assumption', 'feedback visible'))
    def edge(i, kind, source, target): return GraphEdge(i, kind, source, target, 'Lpaper', ())
    graph = ResearchGraph(nodes, (edge('a1', 'addresses', 'm1', 'p'), edge('a2', 'addresses', 'm2', 'p'),
                                  edge('s1', 'assumes', 'm1', 'a'), edge('s2', 'assumes', 'm2', 'a')))
    gap = next(g for g in __import__('src.research_discovery.graph', fromlist=['detect_gaps']).detect_gaps(graph)
               if g.pattern == 'shared_assumption')
    fragment = ResearchGraph((), (edge('h1', 'handles', 'm2', 'a'),))
    updated, still_open = integrate_targeted_read(graph, fragment, gap)
    assert not still_open
    assert any(edge.type == 'handles' for edge in updated.edges)


def test_two_hundred_paper_call_estimate_includes_full_pipeline():
    from src.research_discovery.graph_pipeline import estimate_call_count
    settings = DiscoverySettings()
    assert estimate_call_count(200, 0, settings) == 68


def test_research_gap_output_keeps_failed_edges_out_of_graph(tmp_path):
    papers, review, _ = setup(tmp_path)
    from src.research_discovery.ingest import load_inputs
    document = next(d for d in load_inputs([papers], review) if d.origin == 'paper')
    p = document.passages[0]
    raw = {'nodes': [{'id': 'm', 'type': 'method', 'name': 'Method'},
                     {'id': 'p', 'type': 'problem', 'name': 'Problem'}],
           'edges': [{'id': 'bad', 'type': 'addresses', 'source': 'm', 'target': 'p',
             'source_paper': document.record.id, 'evidence': [{'source': document.record.id,
             'passage': p.id, 'start': 0, 'end': 5, 'quote': 'wrong'}]}]}
    fragment = parse_fragment(raw, [document])
    assert not fragment.edges and fragment.dropped
