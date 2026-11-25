"""Resumable selective-reading and structural-gap workflow."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
import json
import logging
import math
from pathlib import Path
import re

from src.ideation.literature.corpus import build_corpus
from .artifacts import Steps, read_json, write_json
from .assessment import parse_assessment, rank_gaps
from .context import bounded_documents
from .discovery import Hypothesis
from .graph import (GapCandidate, GraphNode, ResearchGraph, canonicalize, detect_gaps,
                    namespace_fragment, parse_fragment, rank_relevance, select_sections)
from .models import Passage, Evidence, ValidationError, identifier, object_fields, text
from .providers import Paper, retrieve
from .report import FORMULA
from .verification import verify_gap

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphGap:
    id: str
    statement: str
    sub_problem: str
    basis: str
    claim_ids: tuple[str, ...]
    why_open: tuple[Hypothesis, ...]
    closing_evidence: str
    closest_prior: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    pattern: str
    family: str
    graph_node_ids: tuple[str, ...]
    graph_edge_ids: tuple[str, ...]


def _prompt(name, data):
    path = Path(__file__).parent / 'prompts' / f'{name}.md'
    return path.read_text() + '\nUNTRUSTED DATA\n' + json.dumps(data, ensure_ascii=False)


def _chunks(values, size):
    return [values[i:i+size] for i in range(0, len(values), size)]


def estimate_call_count(paper_count, manuscript_passages, settings):
    deep_count = min(paper_count, settings.deep_papers)
    abstract_count = max(0, paper_count - deep_count)
    extract_calls = (math.ceil(deep_count / settings.deep_batch) +
        math.ceil(abstract_count / settings.abstract_batch) +
        math.ceil(manuscript_passages / settings.extraction_batch))
    return extract_calls + 2 + settings.max_gaps * 3


def _merge_graphs(graphs):
    nodes, edges, dropped = {}, {}, []
    for graph in graphs:
        for node in graph.nodes:
            prior = nodes.get(node.id)
            if prior:
                # Paper ids are corpus ids shared by every step; agents may title the same paper differently.
                same = prior.type == node.type == 'paper' or (prior.type, prior.name, prior.paper_id) == (node.type, node.name, node.paper_id)
                if not same:
                    raise ValidationError('conflicting graph node id across extraction steps: ' + node.id)
                extra = (node.name,) if node.name != prior.name else ()
                nodes[node.id] = replace(prior, aliases=tuple(dict.fromkeys((*prior.aliases, *node.aliases, *extra))),
                                         tier=prior.tier or node.tier)
            else:
                nodes[node.id] = node
        for edge in graph.edges:
            edges[edge.id] = edge
        dropped.extend(graph.dropped)
    return ResearchGraph(tuple(nodes.values()), tuple(edges.values()), tuple(dropped))


CONCEPT_TYPES = ('problem', 'method', 'assumption', 'failure_condition', 'setting')


def _confirm_canonicalization(graph, steps, generator, topic='', parallel=1):
    """Merge same-concept nodes across extraction calls, one semantic role call per node type.

    Extraction calls run independently and name the same concept differently, so string
    similarity alone leaves the graph fragmented. Findings and papers are not merged.
    """
    preliminary = canonicalize(graph)
    def job(kind):
        members = [n for n in graph.nodes if n.type == kind]
        if len(members) < 2:
            return [{'node_ids': [n.id], 'canonical': n.id, 'name': n.name} for n in members]
        clusters, consumed = [], set()
        for node in preliminary.nodes:
            if node.type != kind:
                continue
            aliases = {node.name, *node.aliases}
            ids = [n.id for n in members if n.id not in consumed and (n.id == node.id or n.name in aliases)]
            if ids:
                clusters.append({'node_ids': ids, 'canonical': ids[0], 'name': node.name})
                consumed.update(ids)
        clusters += [{'node_ids': [n.id], 'canonical': n.id, 'name': n.name} for n in members if n.id not in consumed]
        subset = ResearchGraph(tuple(members), (), ())
        return steps.run('canonicalize', 'canonicalize_' + kind,
                         _prompt('canonicalize', {'topic': topic, 'type': kind, 'clusters': clusters}),
                         lambda result: _validate_clusters(result, subset), agent=generator)
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        groups = [g for result in executor.map(job, CONCEPT_TYPES) for g in result]
    by_old = {n.id: n for n in graph.nodes}
    remap = {n.id: n.id for n in graph.nodes}
    out_nodes = [n for n in graph.nodes if n.type not in CONCEPT_TYPES]
    for group in groups:
        canonical_id = group['canonical']
        aliases = tuple(dict.fromkeys(name for item in group['node_ids'] if item != canonical_id
                                      for name in (by_old[item].name, *by_old[item].aliases)))
        out_nodes.append(replace(by_old[canonical_id], name=group['name'], aliases=aliases))
        remap.update({item: canonical_id for item in group['node_ids']})
    edges = tuple(dict.fromkeys(replace(edge, source=remap[edge.source], target=remap[edge.target])
                                for edge in graph.edges if remap[edge.source] != remap[edge.target]))
    return ResearchGraph(tuple(out_nodes), edges, graph.dropped)


def _validate_clusters(raw, graph):
    object_fields(raw, ('clusters',))
    expected = {node.id for node in graph.nodes}
    seen, result = set(), []
    for item in raw['clusters']:
        object_fields(item, ('node_ids', 'canonical', 'name'))
        ids = item['node_ids']
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in expected for i in ids):
            raise ValidationError('invalid canonical node group')
        if seen.intersection(ids) or item['canonical'] not in ids:
            raise ValidationError('canonical groups must partition node ids and include their canonical')
        kinds = {node.type for node in graph.nodes if node.id in ids}
        if len(kinds) != 1:
            raise ValidationError('canonical groups cannot mix node types')
        seen.update(ids)
        result.append({'node_ids': ids, 'canonical': item['canonical'], 'name': text(item['name'])})
    if seen != expected:
        raise ValidationError('canonicalization omitted graph nodes')
    return tuple(result)


SCREEN_PER_PATTERN, SCREEN_TOTAL = 30, 120


def _topic_settings(topic, graph, max_share=0.1):
    """Topic words that pick out specific settings, not words that occur in most setting names."""
    names = [n.name.casefold() for n in graph.nodes if n.type == 'setting']
    words = dict.fromkeys(w.casefold() for w in re.findall(r'[A-Za-z][A-Za-z0-9-]{3,}', topic))
    return tuple(w for w in words if names and 0 < sum(w in n for n in names) <= max_share * len(names))


COVERAGE_PATTERNS = ('uncovered_combination', 'evaluation_gap')
SYNTHESIS_KINDS = ('shared_assumption', 'unhandled_failure', 'conflicting_findings', 'unbridged_methods',
                   'formulation_gap')


def _synthesis_view(graph, problems=25, per_list=12):
    """Per-problem summary of the graph with node ids, small enough for one synthesis call."""
    nodes = {n.id: n for n in graph.nodes}
    papers_of = {}
    for e in graph.edges:
        papers_of.setdefault(e.source, set()).add(e.source_paper)
        papers_of.setdefault(e.target, set()).add(e.source_paper)
    def item(node_id):
        return {'id': node_id, 'name': nodes[node_id].name, 'papers': len(papers_of.get(node_id, ()))}
    def targets(sources, kind):
        ids = {e.target for e in graph.edges if e.type == kind and e.source in sources and e.target in nodes}
        return [item(i) for i in sorted(ids, key=lambda i: (-len(papers_of.get(i, ())), i))[:per_list]]
    ranked = sorted((n for n in graph.nodes if n.type == 'problem'),
                    key=lambda n: (-len(papers_of.get(n.id, ())), n.id))[:problems]
    view = []
    for problem in ranked:
        methods = {e.source for e in graph.edges if e.type == 'addresses' and e.target == problem.id and e.source in nodes}
        source_papers = {e.source_paper for e in graph.edges if e.source in methods or e.target == problem.id}
        findings = {e.target for e in graph.edges if e.type == 'reports' and e.source in source_papers and e.target in nodes}
        view.append({'problem': item(problem.id),
                     'methods': [item(m) for m in sorted(methods, key=lambda i: (-len(papers_of.get(i, ())), i))[:per_list]],
                     'assumptions': targets(methods, 'assumes'), 'failure_conditions': targets(methods, 'fails_under'),
                     'handled': targets(methods, 'handles'),
                     'findings': [item(f) for f in sorted(findings, key=lambda i: (-len(papers_of.get(i, ())), i))[:per_list]]})
    return view


def _validate_synthesis(raw, graph):
    object_fields(raw, ('gaps',))
    if not isinstance(raw['gaps'], list):
        raise ValidationError('synthesized gaps must be a list')
    nodes = {n.id: n for n in graph.nodes}
    result = []
    for row in raw['gaps']:
        object_fields(row, ('kind', 'statement', 'rationale', 'node_ids'))
        if row['kind'] not in SYNTHESIS_KINDS:
            raise ValidationError('unknown synthesized gap kind')
        ids = [i for i in row['node_ids'] if isinstance(i, str) and i in nodes] if isinstance(row['node_ids'], list) else []
        if not any(nodes[i].type == 'problem' for i in ids):
            continue
        cited = set(ids)
        # Support = edges among cited nodes, plus paper-to-finding reports for cited findings.
        edge_ids = [e.id for e in graph.edges if e.target in cited and
                    (e.source in cited or nodes.get(e.source, GraphNode('', '', '')).type == 'paper')]
        if edge_ids:
            result.append(GapCandidate('synthesis:' + row['kind'],
                                       text(row['statement']) + ' Rationale: ' + text(row['rationale']),
                                       tuple(sorted(cited)), tuple(edge_ids)))
    return result


def _shortlist(candidates, graph, per_pattern, total):
    """Keep the best-supported candidates of each pattern so screening fits one prompt."""
    paper_of = {e.id: e.source_paper for e in graph.edges}
    def support(c):
        return (len({paper_of[e] for e in c.edge_ids if e in paper_of}), len(c.node_ids))
    # Full tie-breaking keeps the screening prompt, and so its cache key, stable across processes.
    def order(c):
        papers, nodes = support(c)
        return (-papers, -nodes, c.pattern, c.statement, c.node_ids, c.edge_ids)
    by_pattern = {}
    for c in sorted(candidates, key=order):
        by_pattern.setdefault(c.pattern, []).append(c)
    kept = [c for group in sorted(by_pattern.items()) for c in group[1][:per_pattern]]
    return sorted(kept, key=order)[:total]


def _validate_screen(raw, candidates):
    object_fields(raw, ('decisions',))
    if not isinstance(raw['decisions'], list):
        raise ValidationError('screen decisions must be a list')
    seen, decisions = set(), []
    for row in raw['decisions']:
        object_fields(row, ('candidate', 'rank', 'accepted', 'reason'))
        index = row['candidate']
        if type(index) is not int or not 0 <= index < len(candidates) or index in seen:
            raise ValidationError('invalid or duplicate screened candidate index')
        if type(row['accepted']) is not bool:
            raise ValidationError('accepted must be boolean')
        if type(row['rank']) is not int or row['rank'] < 1:
            raise ValidationError('screen rank must be a positive integer')
        seen.add(index)
        decisions.append((index, row['rank'], row['accepted'], text(row['reason'])))
    if seen != set(range(len(candidates))):
        raise ValidationError('screen must decide every candidate')
    if len({row[1] for row in decisions}) != len(decisions):
        raise ValidationError('screen ranks must be unique')
    return tuple(sorted(decisions, key=lambda row: row[1]))


def _families(candidates):
    parent = list(range(len(candidates)))
    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    for i, left in enumerate(candidates):
        for j in range(i + 1, len(candidates)):
            if set(left.node_ids) & set(candidates[j].node_ids):
                parent[find(j)] = find(i)
    family_ids = {}
    for index, candidate in enumerate(candidates):
        root = find(index)
        family_ids.setdefault(root, identifier('family_', '|'.join(sorted(candidates[root].node_ids))))
    return [family_ids[find(i)] for i in range(len(candidates))]


def _gap(candidate, graph, family):
    node_map = {n.id: n for n in graph.nodes}
    edge_map = {e.id: e for e in graph.edges}
    edges = [edge_map[i] for i in candidate.edge_ids if i in edge_map]
    evidence = tuple(dict.fromkeys(span for edge in edges for span in edge.evidence))
    papers = tuple(sorted({edge.source_paper for edge in edges}))
    if len(papers) < 2 or not evidence:
        return None
    nodes = [node_map[i] for i in candidate.node_ids if i in node_map]
    problem = next((n for n in nodes if n.type == 'problem'), None)
    statement = candidate.statement
    return GraphGap(identifier('gap_', candidate.pattern + '\n' + statement + '\n' + '|'.join(candidate.node_ids)), statement,
        problem.name if problem else statement, candidate.pattern, (),
        (Hypothesis('The observed pattern may reflect an underexplored condition.', ()),),
        'Test the pattern against relevant methods and evaluation settings.', papers, evidence,
        candidate.pattern, family, candidate.node_ids, candidate.edge_ids)


def integrate_targeted_read(graph, fragment, candidate, *, topic_settings=()):
    """Merge a targeted fragment and indicate whether the exact structural pattern survives."""
    updated = _merge_graphs((graph, fragment))
    still_present = any(item.pattern == candidate.pattern and
                        set(item.node_ids) & set(candidate.node_ids)
                        for item in detect_gaps(updated, topic_settings=topic_settings))
    return updated, still_present


def _report(out, topic, graph, relevance, gaps, verifications, assessments, ranked, documents, audit):
    node_map = {n.id: n for n in graph.nodes}
    write_json(out / 'graph.json', {'nodes': [asdict(n) for n in graph.nodes],
        'edges': [asdict(e) for e in graph.edges], 'dropped_edges': list(graph.dropped)})
    write_json(out / 'relevance.json', {'papers': [asdict(row) for row in relevance]})
    ranks = {row.gap.id: row for row in ranked}
    rows = []
    for gap in gaps:
        verification = verifications[gap.id]
        assessment = assessments.get(gap.id)
        row = {**asdict(gap), 'verification': asdict(verification),
               'assessment': asdict(assessment) if assessment else None,
               'rank': ranks[gap.id].rank if gap.id in ranks else None,
               'score': ranks[gap.id].score if gap.id in ranks else None}
        rows.append(row)
    write_json(out / 'research_gaps.json', {'gaps': rows})
    handoff = []
    for ranking in ranked[:8]:
        gap = ranking.gap
        findings = []
        for edge_id in gap.graph_edge_ids:
            edge = next((edge for edge in graph.edges if edge.id == edge_id), None)
            if edge:
                for span in edge.evidence:
                    source_doc = next((d for d in documents if d.record.id == span.source), None)
                    if source_doc and source_doc.origin == 'paper':
                        findings.append({'citation': span.source,
                            'finding': f'{edge.type}: {node_map[edge.source].name} → {node_map[edge.target].name}'})
        handoff.append({'id': gap.id, 'statement': gap.statement, 'sub_problem': gap.sub_problem,
            'why_open': 'Hypothesis requiring further study.', 'closing_evidence': gap.closing_evidence,
            'closest_prior': list(gap.closest_prior), 'evidence': findings,
            'pattern': gap.pattern, 'family': gap.family})
    write_json(out / 'gaps.json', {'gaps': handoff})
    write_json(out / 'ranked_gaps.json', {'formula': FORMULA, 'gaps': [asdict(row) for row in ranked]})
    write_json(out / 'graph_audit.json', audit)
    lines = ['# Research gap report', '', f'Topic: {topic}', '',
        f'Corpus: {sum(d.origin == "paper" for d in documents)} papers; {len(graph.nodes)} canonical graph nodes; {len(graph.edges)} quote-checked edges.',
        'All edge evidence is checked against exact source passages. Missing search results do not establish novelty.', '',
        '## Ranked gaps', '', FORMULA, '', '| Rank | Pattern | Gap | Verdict | Score |', '| --- | --- | --- | --- | --- |']
    for row in ranked:
        lines.append(f'| {row.rank} | {row.gap.pattern} | {row.gap.statement} | {row.verification.verdict} | {row.score:.2f} |')
    for gap in gaps:
        v = verifications[gap.id]
        assessment = assessments.get(gap.id)
        lines += ['', f'## {gap.statement}', '', f'Pattern: `{gap.pattern}`; family: `{gap.family}`.',
                  f'Verdict: **{v.verdict}** — {v.rationale}', '',
                  'Supporting graph nodes: ' + ', '.join(gap.graph_node_ids),
                  'Supporting graph edges: ' + ', '.join(gap.graph_edge_ids)]
        for edge_id in gap.graph_edge_ids:
            edge = next((edge for edge in graph.edges if edge.id == edge_id), None)
            if edge is None:
                continue
            for span in edge.evidence:
                doc = next((d for d in documents if d.record.id == span.source), None)
                passage = next((p for p in doc.passages if p.id == span.passage), None) if doc else None
                if passage:
                    lines += [f'- Evidence [{span.source}], {doc.record.title}, {passage.section}, paragraph {passage.paragraph}:',
                              '> ' + span.quote.replace('\n', '\n> ')]
        lines += ['', 'Independent searches:']
        lines.extend(f'- {search.query}: {search.status}; results: {", ".join(search.result_ids) or "none"}'
                     + (f'; {search.error}' if search.error else '') for search in v.searches)
        lines.extend('- Uncertainty: ' + note for note in v.uncertainty_notes)
        if assessment:
            lines.append('Research value: ' + '; '.join(
                f'{name} {getattr(assessment, name).score:g}/5 ({getattr(assessment, name).rationale})'
                for name in ('importance', 'difficulty', 'impact', 'feasibility')))
    report = out / 'report.md'
    report.write_text('\n'.join(lines) + '\n')
    (out / 'roadmap.md').write_text('\n'.join(_roadmap_lines(graph, topic, gaps)))
    return report, len(handoff)


def _roadmap_lines(graph, topic, gaps=()):
    node_map = {n.id: n for n in graph.nodes}
    lines = [f'# Research roadmap: {topic}', '']
    problems = [n for n in graph.nodes if n.type == 'problem']
    problems.sort(key=lambda p: (-sum(e.type == 'addresses' and e.target == p.id for e in graph.edges), p.id))
    for problem in problems[:10]:
        methods = {e.source for e in graph.edges if e.type == 'addresses' and e.target == problem.id}
        assumptions = {node_map[e.target].name for e in graph.edges if e.type == 'assumes' and e.source in methods}
        failures = {node_map[e.target].name for e in graph.edges if e.type == 'fails_under' and e.source in methods}
        cells = [gap.statement for gap in gaps if gap.pattern == 'uncovered_combination' and problem.id in gap.graph_node_ids]
        lines += [f'## {problem.name}', '', 'Methods: ' + ', '.join(node_map[m].name for m in sorted(methods)),
            'Shared assumptions: ' + ', '.join(sorted(assumptions)), 'Failure conditions: ' + ', '.join(sorted(failures)),
            'Uncovered cells: ' + (', '.join(cells) or 'none detected'),
            '', '```mermaid', 'graph LR', f'  {problem.id}["{problem.name}"]']
        for method_id in sorted(methods):
            lines.append(f'  {method_id}["{node_map[method_id].name}"] --> {problem.id}')
        lines += ['```', '']
    return lines


def run_graph_discovery(*, topic, papers, manuscript, out, provider, settings, generator, judge, run_role_fn,
                        sources=(), log=print):
    documents = retrieve(topic=topic, papers=papers, manuscript=manuscript, out=out,
                         provider=provider, settings=settings, sources=sources)
    relevance = rank_relevance(topic, documents, settings.deep_papers)
    tier = {row.paper_id: row.tier for row in relevance}
    # Give abstract-tier records without an abstract passage one, so every step can resolve its evidence.
    documents = [replace(d, passages=(Passage(identifier('p_', d.record.id + '\nabstract'), 'Abstract', 1,
                                              d.record.abstract.strip()), *d.passages))
                 if d.origin == 'paper' and tier.get(d.record.id) == 'abstract' and d.record.abstract.strip()
                 and not any('abstract' in p.section.casefold() for p in d.passages) else d
                 for d in documents]
    write_json(out / 'relevance.json', {'papers': [asdict(row) for row in relevance]})
    steps = Steps(out / 'steps', generator, run_role_fn)
    steps.timeout = settings.role_timeout
    papers_by_id = {d.record.id: d for d in documents if d.origin == 'paper'}
    deep = []
    abstract = []
    for row in relevance:
        doc = papers_by_id[row.paper_id]
        if row.tier == 'deep':
            deep.append(replace(doc, passages=select_sections(doc)))
        else:
            aps = [p for p in doc.passages if 'abstract' in p.section.casefold()]
            selected = tuple(aps[:1])
            abstract.append(replace(doc, passages=selected))
    manuscript_docs = [d for d in documents if d.origin == 'manuscript']
    manuscript_parts = [replace(d, passages=tuple(part)) for d in manuscript_docs
                        for part in _chunks(list(d.passages), settings.extraction_batch)]
    deep_batches = _chunks(deep, settings.deep_batch)
    abstract_batches = _chunks(abstract, settings.abstract_batch)
    extraction_count = len(deep_batches) + len(abstract_batches) + len(manuscript_parts)
    estimate = estimate_call_count(len(papers_by_id), sum(len(d.passages) for d in manuscript_docs), settings)
    log(f'Estimated LLM calls before extraction: {estimate} (selective extraction {extraction_count}, canonicalization 1, screening 1, verification/assessment/targeted-reading up to {settings.max_gaps * 3}).')
    jobs = []
    jobs.extend(('deep', i, batch) for i, batch in enumerate(deep_batches))
    jobs.extend(('abstract', i, batch) for i, batch in enumerate(abstract_batches))
    jobs.extend(('manuscript', i, [doc]) for i, doc in enumerate(manuscript_parts))
    def extract_job(job):
        kind, index, batch = job
        prompt = _prompt('graph_extract', {'topic': topic, 'documents': [asdict(d) for d in batch]})
        # Manuscript citation evidence may resolve to a paper outside this call's batch.
        docs = documents
        step = f'graph_extract_{kind}_{index:05d}'
        return steps.run('graph_extract', step, prompt,
                         lambda raw: namespace_fragment(parse_fragment(raw, docs), step))
    with ThreadPoolExecutor(max_workers=settings.parallel) as executor:
        fragments = list(executor.map(extract_job, jobs))
    graph = _merge_graphs(fragments)
    for issue in graph.dropped:
        LOGGER.warning('%s', issue)
    # Paper records are explicit graph nodes with relevance tiers.
    paper_nodes = [GraphNode(d.record.id, 'paper', d.record.title, (), d.record.id, tier[d.record.id]) for d in papers_by_id.values()]
    merged_nodes = {node.id: node for node in graph.nodes}
    merged_nodes.update({node.id: node for node in paper_nodes})
    graph = ResearchGraph(tuple(merged_nodes.values()), graph.edges, graph.dropped)
    graph = _confirm_canonicalization(graph, steps, generator, topic, settings.parallel)
    # Re-run detector set after targeted reading; each gap gets at most one cached role step.
    # Selective reading skips experiment sections, so "never evaluated on" patterns mostly reflect
    # unread evaluations rather than missing work; screen only patterns selective reading supports.
    candidates = [c for c in detect_gaps(graph, topic_settings=_topic_settings(topic, graph))
                  if c.pattern not in COVERAGE_PATTERNS]
    candidates = _shortlist(candidates, graph, SCREEN_PER_PATTERN, SCREEN_TOTAL)
    view = _synthesis_view(graph)
    synthesized = steps.run('synthesize', 'synthesize', _prompt('synthesize', {'topic': topic, 'roadmap': view}),
                            lambda raw: _validate_synthesis(raw, graph), agent=generator)
    candidates = [*synthesized, *candidates]
    log(f'Screening {len(synthesized)} synthesized and {len(candidates) - len(synthesized)} structural candidates.')
    screened = steps.run('screen', 'screen', _prompt('screen', {'topic': topic, 'candidates': [
        {'candidate': i, 'pattern': c.pattern, 'statement': c.statement,
         'supporting_papers': len({e.source_paper for e in graph.edges if e.id in set(c.edge_ids)})}
        for i, c in enumerate(candidates)]}),
                         lambda raw: _validate_screen(raw, candidates), agent=judge)
    write_json(out / 'screen.json', {'decisions': [{'candidate': i, 'rank': rank,
        'accepted': ok, 'reason': reason, 'pattern': candidates[i].pattern,
        'statement': candidates[i].statement} for i, rank, ok, reason in screened]})
    accepted = [candidates[index] for index, _rank, ok, _reason in screened if ok][:settings.max_gaps]
    families = _families(accepted)
    gaps = [gap for candidate, family in zip(accepted, families) if (gap := _gap(candidate, graph, family))]
    # Targeted reads inspect methods/results only for abstract-tier sources supporting each candidate.
    targeted_fulltext_errors = []
    for gap in list(gaps):
        source_ids = {edge.source_paper for edge in graph.edges if edge.id in gap.graph_edge_ids and
                      tier.get(edge.source_paper) == 'abstract'}
        targeted_docs = []
        for source_id in sorted(source_ids)[:settings.max_targeted_papers]:
            doc = papers_by_id[source_id]
            try:
                full = provider.fulltext(Paper(doc.record), out / 'targeted')
            except (OSError, ValueError) as exc:
                LOGGER.warning('Targeted full-text retrieval failed for %s: %s', source_id, exc)
                targeted_fulltext_errors.append({'source': source_id, 'error': str(exc)})
                full = ''
            if full:
                from .ingest import passages
                full_doc = replace(doc, passages=passages(full, source_id, max_chars=settings.passage_chars),
                                   fulltext_status='fulltext_pdf')
                selected = tuple(p for p in full_doc.passages if any(word in p.section.casefold() for word in ('method', 'result')))
                targeted_docs.append(replace(full_doc, passages=selected))
            else:
                targeted_docs.append(replace(doc, passages=tuple(p for p in doc.passages if any(w in p.section.casefold() for w in ('method', 'result')))))
        if not targeted_docs:
            continue
        for targeted_doc in targeted_docs:
            old = papers_by_id[targeted_doc.record.id]
            passage_map = {p.id: p for p in (*old.passages, *targeted_doc.passages)}
            updated_doc = replace(old, passages=tuple(passage_map.values()),
                                  fulltext_status=targeted_doc.fulltext_status)
            papers_by_id[targeted_doc.record.id] = updated_doc
            documents = [updated_doc if d.record.id == updated_doc.record.id else d for d in documents]
        known_node_ids = set(gap.graph_node_ids)
        known_nodes = [asdict(node) for node in graph.nodes if node.id in known_node_ids]
        prompt = _prompt('graph_extract', {'topic': topic, 'gap': asdict(gap),
            'known_nodes': known_nodes, 'documents': [asdict(d) for d in targeted_docs]})
        extra = steps.run('targeted_read', 'targeted_' + gap.id, prompt,
                          lambda raw: namespace_fragment(parse_fragment(
                              raw, targeted_docs, [n for n in graph.nodes if n.id in known_node_ids]), 'targeted_' + gap.id))
        for issue in extra.dropped:
            LOGGER.warning('%s', issue)
        graph, survives = integrate_targeted_read(graph, extra,
            type('Candidate', (), {'pattern': gap.pattern, 'node_ids': gap.graph_node_ids})(),
            topic_settings=_topic_settings(topic, graph))
        # Synthesized gaps are not detector patterns, so only detector gaps are re-checked here.
        if not survives and not gap.pattern.startswith('synthesis:'):
            gaps.remove(gap)
    verifications, assessments = {}, {}
    for gap in gaps:
        verification, documents = verify_gap(gap, documents, provider=provider, steps=steps, out=out,
                                             settings=settings, judge=judge)
        verifications[gap.id] = verification
        if verification.verdict in ('open', 'partially_addressed'):
            relevant = bounded_documents([d for d in documents if d.record.id in set(gap.closest_prior)],
                                         topic + ' ' + gap.statement, required=gap.evidence,
                                         limit=settings.context_passages)
            data = {'topic': topic, 'gap': asdict(gap), 'verification': asdict(verification),
                    'documents': [asdict(d) for d in relevant]}
            assessments[gap.id] = steps.run('assess', 'assess_' + gap.id, _prompt('assess', data),
                lambda raw, gap=gap, relevant=relevant: parse_assessment(raw, gap_id=gap.id, documents=relevant), agent=judge)
    ranked = rank_gaps(gaps, verifications, assessments)
    records = [d.record for d in documents if d.origin == 'paper']
    build_corpus(out / 'corpus', ['folder:discovery-final-snapshot'], require_nonempty=True,
                 fetchers={'folder': lambda _: records})
    write_json(out / 'documents.json', {'documents': [asdict(d) for d in documents]})
    audit = read_json(out / 'retrieval/search_audit.json')
    audit['targeted_fulltext_errors'] = targeted_fulltext_errors
    audit['verification_searches'] = {gap_id: [asdict(search) for search in verification.searches]
                                      for gap_id, verification in verifications.items()}
    report, handoff_count = _report(out, topic, graph, relevance, gaps, verifications, assessments,
                                    ranked, documents, audit)
    write_json(out / 'status.json', {'state': 'completed' if ranked else 'no_supported_gaps',
        'candidate_gaps': len(gaps), 'ranked_gaps': len(ranked), 'handoff_count': handoff_count,
        'estimated_calls': estimate, 'report': str(report)})
    return report
