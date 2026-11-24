from pathlib import Path
import pytest
from src.ideation.backends import get_backend
from src.investigation.config import InvestigationConfig
from src.investigation.config_loader import load_config
from src.investigation.parallel_helpers import build_parallel_worker_command


def test_default_backend_keeps_runner():
    backend=get_backend('sakana-scientist-v3'); runner=lambda **kw:None
    assert backend.idea_generation_fn(default=runner) is runner
    backend.record_outcome(Path('/unused'),idea_id='x')


def test_unknown_backend_lists_choices():
    with pytest.raises(ValueError,match='coscientist, sakana-scientist-v3'): get_backend('unknown')
    with pytest.raises(ValueError,match='unknown ideation.backend'): load_config(['ideation.backend=unknown'])


def test_coscientist_backend_requires_sources():
    with pytest.raises(ValueError,match='sources.*corpus_dir'): load_config(['ideation.backend=coscientist'])


def test_coscientist_backend_forces_preregistration(caplog):
    cfg=load_config(['ideation.backend=coscientist','ideation.literature.sources=[folder:/tmp]'])
    assert cfg.execution.require_preregistration and 'ideation_forced_preregistration' in caplog.text


def test_bad_source_spec_is_reported():
    with pytest.raises(ValueError,match='bogus:q'): load_config(['ideation.literature.sources=[bogus:q]'])


def test_parallel_workers_receive_corpus_dir(tmp_path):
    cfg=InvestigationConfig(); cfg.ideation.backend='coscientist'; cfg.ideation.literature.corpus_dir=str(tmp_path/'literature')
    cmd=build_parallel_worker_command(cfg=cfg,task_dir=tmp_path,instruct_path=tmp_path/'i',results_dir=tmp_path/'worker',global_memory_dir=tmp_path,assigned_cycles=2)
    assert any('ideation.literature.corpus_dir=' in part and str(tmp_path/'literature') in part for part in cmd)
    assert 'ideation.backend="coscientist"' in cmd or 'ideation.backend=coscientist' in cmd


def test_prepare_folder_corpus_and_reuse(tmp_path):
    cfg=InvestigationConfig(); cfg.ideation.backend='coscientist'
    folder=tmp_path/'papers'; folder.mkdir(); (folder/'a.md').write_text('# A')
    cfg.ideation.literature.sources=[f'folder:{folder}']
    backend=get_backend('coscientist'); backend.prepare(cfg,results_dir=tmp_path/'results',log_event_fn=lambda *a,**k:None)
    assert Path(cfg.ideation.literature.corpus_dir,'manifest.json').is_file()
    backend2=get_backend('coscientist'); backend2.prepare(cfg,results_dir=tmp_path/'worker',log_event_fn=lambda *a,**k:None)
    assert not (tmp_path/'worker'/'literature').exists()
