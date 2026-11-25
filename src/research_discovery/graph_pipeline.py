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
from .graph import (GraphNode, ResearchGraph, canonicalize, detect_gaps,
                    namespace_fragment, parse_fragment, rank_relevance, select_sections)
from .models import Evidence, ValidationError, identifier, object_fields, text
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
                if (prior.type, prior.name, prior.paper_id) != (node.type, node.name, node.paper_id):
                    raise ValidationError('conflicting graph node id across extraction steps: ' + node.id)
                nodes[node.id] = replace(prior, aliases=tuple(dict.fromkeys((*prior.aliases, *node.aliases))),
                                         tier=prior.tier or node.tier)
            else:
                nodes[node.id] = node
        for edge in graph.edges:
            edges[edge.id] = edge
        dropped.extend(graph.dropped)
    return ResearchGraph(tuple(nodes.values()), tuple(edges.values()), tuple(dropped))


def _confirm_canonicalization(graph, steps, generator):
    preliminary = canonicalize(graph)
    # Send deterministic clusters to one strict role call. Its output may split clusters,
    # but must partition the complete input node set exactly once.
    clusters = []
    consumed = set()
    for node in preliminary.nodes:
        aliases = {node.name, *node.aliases}
        members = [n.id for n in graph.nodes if n.type == node.type and (n.id == node.id or n.name in aliases)]
        clusters.append({'node_ids': members, 'canonical': node.id, 'name': node.name})
        consumed.update(members)
    for node in graph.nodes:
        if node.id not in consumed:
            clusters.append({'node_ids': [node.id], 'canonical': node.id, 'name': node.name})
    raw = steps.run('canonicalize', 'canonicalize', _prompt('canonicalize', {'clusters': clusters}),
                    lambda result: _validate_clusters(result, graph))
    by_old = {n.id: n for n in graph.nodes}
    remap, out_nodes = {}, []
    for group in raw:
        canonical_id = group['canonical']
        node = by_old[canonical_id]
        aliases = tuple(dict.fromkeys(by_old[item].name for item in group['node_ids'] if item != canonical_id))
        out_nodes.append(replace(node, name=group['name'], aliases=aliases))
        remap.update({item: canonical_id for item in group['node_ids']})
    edges = tuple(replace(edge, source=remap[edge.source], target=remap[edge.target]) for edge in graph.edges
                  if remap[edge.source] != remap[edge.target])
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
            if aps:
                selected = tuple(aps[:1])
            elif doc.record.abstract.strip():
                from .models import Passage
                selected = (Passage(identifier('p_', doc.record.id + '\nabstract'), 'Abstract', 1,
                                    doc.record.abstract.strip()),)
            else:
                selected = ()
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
    graph = _confirm_canonicalization(graph, steps, generator)
    # Re-run detector set after targeted reading; each gap gets at most one cached role step.
    candidates = list(detect_gaps(graph, topic_settings=tuple(w.casefold() for w in re.findall(r'[A-Za-z]{4,}', topic))))
    screened = steps.run('screen', 'screen', _prompt('screen', {'topic': topic, 'candidates': [asdict(c) for c in candidates]}),
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
                          lambda raw: parse_fragment(raw, targeted_docs))
        for issue in extra.dropped:
            LOGGER.warning('%s', issue)
        graph, survives = integrate_targeted_read(graph, extra,
            type('Candidate', (), {'pattern': gap.pattern, 'node_ids': gap.graph_node_ids})(),
            topic_settings=tuple(w.casefold() for w in re.findall(r'[A-Za-z]{4,}', topic)))
        if not survives:
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
