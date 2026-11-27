from src.research_discovery import cli


def test_cli_parser_and_dispatch(tmp_path,monkeypatch,capsys):
    from src.agsci.main import main
    seen=[]
    def run(**kwargs):seen.append(kwargs);return tmp_path/'report.md'
    monkeypatch.setattr(cli,'run_discovery',run)
    assert main(['discover','run','--topic','Policies','--papers','papers.bib','--manuscript','review.md','--out',str(tmp_path),'--offline'])==0
    assert seen[0]['settings'].offline and seen[0]['papers']==['papers.bib']
    assert 'report.md' in capsys.readouterr().out
