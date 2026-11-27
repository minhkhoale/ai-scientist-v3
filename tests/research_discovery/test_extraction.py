import pytest
from src.research_discovery.ingest import load_inputs
from src.research_discovery.extraction import extract_claims
from src.research_discovery.models import ValidationError


def fixture(tmp_path):
    papers=tmp_path/'papers';papers.mkdir()
    (papers/'a.md').write_text('# Method A\n\n## Limitations\nLong rollouts remain unstable.')
    review=tmp_path/'review.md';review.write_text('# Review\n\n## Gaps\nLong rollouts remain unstable [@a].')
    return load_inputs([papers],review)


def test_manuscript_claim_carries_section_paragraph_and_citation(tmp_path):
    docs=fixture(tmp_path)
    paper=next(d for d in docs if d.origin=='paper')
    review=next(d for d in docs if d.origin=='manuscript')
    p=review.passages[0]
    assert p.section=='Gaps' and p.paragraph==1 and p.citations==(paper.record.id,)
    raw={'claims':[{'kind':'limitation','statement':'Review reports unstable rollouts','uncertainty':'uncertain',
                   'evidence':[dict(source=review.record.id,passage=p.id,start=0,end=28,quote=p.text[:28])]}]}
    result=extract_claims(raw,docs)
    assert result.claims[0].evidence[0].quote==p.text[:28]
    assert result.claims[0].citations==(paper.record.id,)


@pytest.mark.parametrize('bad', ['quote','source','offset'])
def test_fabricated_support_is_dropped(tmp_path,bad):
    docs=fixture(tmp_path);d=docs[0];p=d.passages[0]
    e=dict(source=d.record.id,passage=p.id,start=0,end=len(p.text),quote=p.text)
    if bad=='quote':e['quote']='Invented result'
    if bad=='source':e['source']='Lunknown'
    if bad=='offset':e['start']=True
    result=extract_claims({'claims':[dict(kind='finding',statement='Claim',uncertainty='reported',evidence=[e])]},docs)
    assert not result.claims and result.dropped


def test_strict_boundary_and_unresolved_review_citations(tmp_path):
    docs=fixture(tmp_path)
    with pytest.raises(ValidationError):extract_claims({'claims':'bad'},docs)
    with pytest.raises(ValidationError):extract_claims({'claims':[dict(kind='guess',statement='x',uncertainty='reported',evidence=[])]},docs)
    review=tmp_path/'review.md';review.write_text('# R\n\nA claim [@missing].')
    d=load_inputs([],review)[0];p=d.passages[0]
    result=extract_claims({'claims':[dict(kind='finding',statement='Claim',uncertainty='reported',evidence=[dict(source=d.record.id,passage=p.id,start=0,end=len(p.text),quote=p.text)])]},[d])
    assert result.claims[0].uncertainty=='uncertain'


def test_latex_bibtex_and_pdf_inputs(tmp_path):
    bib=tmp_path/'papers.bib';bib.write_text('@article{smith, title={A nested {method}}, author={A Smith}, doi={10.1/example}}')
    pdf=tmp_path/'paper.pdf';pdf.write_bytes(b'%PDF-fixture')
    review=tmp_path/'review.tex';review.write_text('\\section{Limits}\nA limitation \\cite{smith}.')
    docs=load_inputs([bib,pdf],review,pdf_text=lambda p:'Page one\n\n## Findings\nVerified result.')
    assert len(docs)==3
    assert next(d for d in docs if d.origin=='manuscript').passages[0].citations
    assert any('Verified result.' in p.text for d in docs for p in d.passages)


def test_numeric_review_references_resolve_to_seed(tmp_path):
    papers=tmp_path/'papers';papers.mkdir();(papers/'a.md').write_text('# A robust method\n\nA limitation.')
    review=tmp_path/'review.md';review.write_text('# R\n\n## Analysis\nThe method is limited [1].\n\n## References\n[1] A robust method.')
    docs=load_inputs([papers],review)
    p=next(d for d in docs if d.origin=='manuscript').passages[0]
    assert p.citations==(docs[0].record.id,)


def test_duplicate_papers_do_not_count_as_independent_sources(tmp_path):
    papers=tmp_path/'papers';papers.mkdir()
    for key in ('a','b'):(papers/(key+'.md')).write_text('# Same paper\n\nA limitation.')
    review=tmp_path/'review.md';review.write_text('# Review\n\nA claim [@a; @b].')
    docs=load_inputs([papers],review)
    assert len([d for d in docs if d.origin=='paper'])==1
    assert len(docs[-1].passages[0].citations)==1


def test_pdf_section_and_paragraph_are_retained(tmp_path):
    pdf=tmp_path/'paper.pdf';pdf.write_bytes(b'%PDF-')
    review=tmp_path/'review.md';review.write_text('# Review\n\nA claim.')
    docs=load_inputs([pdf],review,pdf_text=lambda p:'1 Introduction\n\nA finding.\n\n2 Limitations\n\nA limitation.')
    assert docs[0].passages[1].section=='2 Limitations'
    assert docs[0].passages[1].paragraph==1


def test_latex_citation_notes_and_emails_do_not_corrupt_citations(tmp_path):
    bib=tmp_path/'papers.bib';bib.write_text('@article{smith, title={A method}, url={https://example.org/a}}')
    review=tmp_path/'review.tex';review.write_text('\\section{Limits}\nContact a@example.org. A limitation \\citep[Section 2]{smith}.')
    docs=load_inputs([bib],review);p=docs[-1].passages[0]
    assert p.citations==(docs[0].record.id,) and not p.unresolved
