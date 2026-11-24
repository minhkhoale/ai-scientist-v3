"""Where a native harness evaluation runs: locally, on a Slurm job, or on a new allocation."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from src.investigation.evaluator_launcher import (
    LauncherError,
    LauncherSettings,
    launch_environment,
    planned_launch,
    running_jobs,
    settings_from_environment,
)
from src.investigation.harness_eval import bootstrap_native_incumbent
from tests.program_testutils import make_program_dir

COMMAND = ["/venv/python", "/run/evaluate.py", "--results_dir", "/run/p"]
JOBS = "101|interactive|gpu-large\n102|train-long|gpu\n103|interact-a100|gpu\n"


def _squeue(output: str = JOBS):
    return lambda arguments: output


def _settings(tmp_path: Path, **changes: object) -> LauncherSettings:
    values: dict[str, object] = {"lock_dir": tmp_path / "locks"}
    values.update(changes)
    return LauncherSettings(**values)  # type: ignore[arg-type]


# --- settings ----------------------------------------------------------------------------------


def test_settings_default_to_local_and_parse_slurm_options() -> None:
    assert settings_from_environment({}).mode == "local"
    settings = settings_from_environment(
        {
            "AGSCI_EVAL_LAUNCHER": "slurm-alloc",
            "AGSCI_EVAL_SLURM_PARTITION": "gpu-large",
            "AGSCI_EVAL_SLURM_GRES": "",
            "AGSCI_EVAL_SLURM_CPUS": "4",
            "AGSCI_EVAL_SLURM_EXTRA_ARGS": "--qos=short --constraint='h100 | h200'",
        }
    )
    assert (settings.mode, settings.partition, settings.gres, settings.cpus) == (
        "slurm-alloc", "gpu-large", None, 4,
    )
    assert settings.extra_args == ("--qos=short", "--constraint=h100 | h200")


@pytest.mark.parametrize(
    "environment",
    [
        {"AGSCI_EVAL_LAUNCHER": "ssh"},
        {"AGSCI_EVAL_LAUNCHER": "slurm-job"},
        {"AGSCI_EVAL_LAUNCHER": "slurm-job", "AGSCI_EVAL_SLURM_JOBID": "abc"},
        {"AGSCI_EVAL_LAUNCHER": "auto", "AGSCI_EVAL_SLURM_JOB_NAME_REGEX": "("},
        {"AGSCI_EVAL_LAUNCHER": "auto", "AGSCI_EVAL_SLURM_CPUS": "many"},
        {"AGSCI_EVAL_LAUNCHER": "auto", "AGSCI_EVAL_SLURM_EXTRA_ARGS": "'unterminated"},
    ],
)
def test_invalid_settings_are_rejected(environment: dict[str, str]) -> None:
    with pytest.raises(LauncherError):
        settings_from_environment(environment)


# --- placement ---------------------------------------------------------------------------------


def test_local_mode_leaves_the_command_alone(tmp_path: Path) -> None:
    with planned_launch(COMMAND, settings=_settings(tmp_path), timeout_seconds=60) as plan:
        assert plan.command == COMMAND and plan.description == {"mode": "local"}


def test_alloc_mode_requests_a_new_allocation_bounded_by_the_timeout(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="slurm-alloc", partition="gpu-large", account="lab")
    with planned_launch(COMMAND, settings=settings, timeout_seconds=600) as plan:
        assert plan.command[0] == "srun" and "--jobid=101" not in plan.command
        assert {"--time=12", "--gres=gpu:1", "--partition=gpu-large", "--account=lab"} <= set(plan.command)
        assert plan.command[plan.command.index("--") + 1 :] == COMMAND


def test_job_mode_runs_a_step_inside_a_running_job(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="slurm-job", jobid="101")
    with planned_launch(COMMAND, settings=settings, timeout_seconds=60, squeue=_squeue()) as plan:
        assert plan.command[:3] == ["srun", "--jobid=101", "--overlap"]
        assert plan.command[plan.command.index("--") + 1 :] == COMMAND


def test_job_mode_refuses_a_job_that_is_not_running(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="slurm-job", jobid="999")
    with pytest.raises(LauncherError, match="999 is not one of your running jobs"):
        with planned_launch(COMMAND, settings=settings, timeout_seconds=60, squeue=_squeue()):
            raise AssertionError("must not run")


def test_evaluations_on_the_same_job_take_turns(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="slurm-job", jobid="101")
    with planned_launch(COMMAND, settings=settings, timeout_seconds=60, squeue=_squeue()):
        with pytest.raises(LauncherError, match="stayed busy"):
            with planned_launch(COMMAND, settings=settings, timeout_seconds=0, squeue=_squeue()):
                raise AssertionError("second evaluation must wait")
    with planned_launch(COMMAND, settings=settings, timeout_seconds=60, squeue=_squeue()) as plan:
        assert plan.command[1] == "--jobid=101"  # released after the first block


def test_auto_prefers_a_free_matching_job_then_falls_back_to_a_new_allocation(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="auto")
    with planned_launch(COMMAND, settings=settings, timeout_seconds=60, squeue=_squeue()) as first:
        assert first.description["jobid"] == "101"
        with planned_launch(COMMAND, settings=settings, timeout_seconds=60, squeue=_squeue()) as second:
            assert second.description["jobid"] == "103"  # 102 does not match the name pattern
            with planned_launch(COMMAND, settings=settings, timeout_seconds=60, squeue=_squeue()) as third:
                assert third.description["mode"] == "slurm-alloc"


def test_auto_honours_the_partition_filter(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="auto", partition="gpu")
    with planned_launch(COMMAND, settings=settings, timeout_seconds=60, squeue=_squeue()) as plan:
        assert plan.description["jobid"] == "103"


def test_running_jobs_ignores_malformed_lines() -> None:
    assert running_jobs(_squeue("garbage\n7|name|part\n|x|y\n")) == [("7", "name", "part")]


def test_launcher_drops_enclosing_job_variables(tmp_path: Path) -> None:
    environment = {"PATH": "/bin", "SLURM_JOB_ID": "5", "SLURM_NTASKS": "1", "CUDA_VISIBLE_DEVICES": "0"}
    with planned_launch(COMMAND, settings=_settings(tmp_path, mode="slurm-alloc"), timeout_seconds=60) as plan:
        assert launch_environment(environment, plan) == {"PATH": "/bin", "CUDA_VISIBLE_DEVICES": "0"}
    with planned_launch(COMMAND, settings=_settings(tmp_path), timeout_seconds=60) as plan:
        assert launch_environment(environment, plan) == environment


# --- through the harness -----------------------------------------------------------------------


def _fake_slurm(bin_dir: Path, log: Path, jobs: str = "7|interactive|gpu\n") -> None:
    bin_dir.mkdir()
    srun = bin_dir / "srun"
    srun.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': argv, 'slurm_vars': [k for k in os.environ if k.startswith('SLURM_')]}}) + '\\n')\n"
        "os.execvp(argv[argv.index('--') + 1], argv[argv.index('--') + 1:])\n"
    )
    squeue = bin_dir / "squeue"
    squeue.write_text(f"#!/bin/sh\nprintf '%s' {jobs!r}\n")
    for script in (srun, squeue):
        script.chmod(script.stat().st_mode | stat.S_IXUSR)


def _evaluator(root: Path) -> None:
    (root / "evaluate.py").write_text(
        "import argparse, json, pathlib\n"
        "p = argparse.ArgumentParser(); p.add_argument('--program_path'); p.add_argument('--results_dir')\n"
        "a = p.parse_args(); out = pathlib.Path(a.results_dir)\n"
        "(out / 'metrics.json').write_text(json.dumps({'combined_score': 1.0}))\n"
        "(out / 'correct.json').write_text(json.dumps({'correct': True}))\n"
    )


def _bootstrap(tmp_path: Path, environment: dict[str, str]) -> tuple[int, Path, list[dict]]:
    root = tmp_path / "run"
    seed = root / ".baseline-seed-x" / "program_0"
    make_program_dir(seed, content="def solve():\n    return 1\n")
    _evaluator(root)
    log = tmp_path / "srun.log"
    _fake_slurm(tmp_path / "bin", log)
    path = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    code = bootstrap_native_incumbent(
        program_dir=seed, task_dir=root, python_bin=sys.executable, timeout_seconds=30,
        environment={"PATH": path, **environment}, gpu_evaluation=True,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return code, seed, calls


def test_harness_sends_the_evaluator_through_srun_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    code, seed, calls = _bootstrap(
        tmp_path,
        {"AGSCI_EVAL_LAUNCHER": "slurm-job", "AGSCI_EVAL_SLURM_JOBID": "7",
         "AGSCI_EVAL_SLURM_LOCK_DIR": str(tmp_path / "locks"), "SLURM_JOB_ID": "5"},
    )
    assert code == 0 and (seed / "metrics.json").is_file()
    assert len(calls) == 1 and calls[0]["argv"][:3] == ["--jobid=7", "--overlap", "--export=ALL"]
    assert calls[0]["slurm_vars"] == []  # the enclosing job's variables never reach srun
    manifest = json.loads((seed / "evaluation_manifest.json").read_text())
    assert manifest["launcher"]["jobid"] == "7" and manifest["launched_command"][0] == "srun"
    assert manifest["command"][0] == sys.executable  # the evaluator command itself is unchanged


def test_harness_without_a_launcher_does_not_call_srun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, seed, calls = _bootstrap(tmp_path, {})
    assert code == 0 and calls == []
    assert "launcher" not in json.loads((seed / "evaluation_manifest.json").read_text())


def test_harness_stops_with_a_clear_message_when_the_job_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    with pytest.raises(SystemExit, match="Slurm job 99 is not one of your running jobs"):
        _bootstrap(
            tmp_path,
            {"AGSCI_EVAL_LAUNCHER": "slurm-job", "AGSCI_EVAL_SLURM_JOBID": "99",
             "AGSCI_EVAL_SLURM_LOCK_DIR": str(tmp_path / "locks")},
        )
    manifest = json.loads(next(tmp_path.rglob("evaluation_manifest.json")).read_text())
    assert manifest["exit_code"] == 125 and "99" in manifest["launcher_error"]
