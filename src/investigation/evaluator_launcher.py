"""Where a native harness evaluation runs: here, on an existing Slurm job, or on a new allocation.

The agent loop and the evaluator can live on different hosts. A login node keeps the agent (which
may only reach its API from there) and sends only the evaluator command to a GPU node through
``srun``. The choice is host policy, not science, so it comes from the environment:

``AGSCI_EVAL_LAUNCHER``
    ``local`` (default): run the evaluator in this process tree, as before.
    ``slurm-job``: run it as an ``srun --overlap`` step inside the running job
    ``AGSCI_EVAL_SLURM_JOBID`` (for example your interactive job). Evaluations that target the
    same job take turns.
    ``slurm-alloc``: ask Slurm for a new allocation for each evaluation (``srun`` blocks while the
    job waits in the queue; the queue wait counts against the harness evaluation timeout).
    ``auto``: use a running job of yours whose name matches ``AGSCI_EVAL_SLURM_JOB_NAME_REGEX``
    and that no other evaluation holds, otherwise fall back to ``slurm-alloc``.

Other settings (all optional): ``AGSCI_EVAL_SLURM_PARTITION``, ``AGSCI_EVAL_SLURM_ACCOUNT``,
``AGSCI_EVAL_SLURM_GRES`` (default ``gpu:1``; empty for none), ``AGSCI_EVAL_SLURM_CPUS``,
``AGSCI_EVAL_SLURM_MEM``, ``AGSCI_EVAL_SLURM_EXTRA_ARGS`` (shell-quoted ``srun`` flags),
``AGSCI_EVAL_SLURM_JOB_NAME_REGEX`` (default ``interact``), ``AGSCI_EVAL_SLURM_LOCK_DIR``.

The run directory, the evaluator, and the Python interpreter must be visible at the same path
on the target node (a shared filesystem).
"""

from __future__ import annotations

import fcntl
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("local", "slurm-job", "slurm-alloc", "auto")
DEFAULT_JOB_NAME_REGEX = "interact"
LOCK_POLL_SECONDS = 2.0
JOB_ENV_PREFIX = "SLURM_"


class LauncherError(RuntimeError):
    """The evaluation could not be placed; nothing ran."""


@dataclass(frozen=True)
class LauncherSettings:
    mode: str = "local"
    jobid: str | None = None
    job_name_regex: str = DEFAULT_JOB_NAME_REGEX
    partition: str | None = None
    account: str | None = None
    gres: str | None = "gpu:1"
    cpus: int | None = None
    mem: str | None = None
    extra_args: tuple[str, ...] = ()
    lock_dir: Path = field(default_factory=lambda: Path(f"/tmp/agsci-eval-launch-{os.getuid()}"))


@dataclass(frozen=True)
class LaunchPlan:
    command: list[str]
    description: dict[str, object]
    drop_environment_prefix: str | None = None


def settings_from_environment(environment: Mapping[str, str]) -> LauncherSettings:
    """Parse ``AGSCI_EVAL_*`` variables. Raises ``LauncherError`` on an invalid combination."""

    def text(name: str) -> str | None:
        value = environment.get(f"AGSCI_EVAL_{name}")
        return value.strip() or None if value is not None else None

    mode = text("LAUNCHER") or "local"
    if mode not in MODES:
        raise LauncherError(f"AGSCI_EVAL_LAUNCHER must be one of {', '.join(MODES)}; got {mode!r}")
    jobid = text("SLURM_JOBID")
    if mode == "slurm-job" and not (jobid and jobid.isdigit()):
        raise LauncherError("AGSCI_EVAL_LAUNCHER=slurm-job needs a numeric AGSCI_EVAL_SLURM_JOBID")
    regex = text("SLURM_JOB_NAME_REGEX") or DEFAULT_JOB_NAME_REGEX
    try:
        re.compile(regex)
    except re.error as exc:
        raise LauncherError(f"invalid AGSCI_EVAL_SLURM_JOB_NAME_REGEX: {exc}") from exc
    cpus_text = text("SLURM_CPUS")
    if cpus_text is not None and not cpus_text.isdigit():
        raise LauncherError("AGSCI_EVAL_SLURM_CPUS must be a positive integer")
    gres_raw = environment.get("AGSCI_EVAL_SLURM_GRES")
    gres = "gpu:1" if gres_raw is None else (gres_raw.strip() or None)
    extra = text("SLURM_EXTRA_ARGS")
    try:
        extra_args = tuple(shlex.split(extra)) if extra else ()
    except ValueError as exc:
        raise LauncherError(f"invalid AGSCI_EVAL_SLURM_EXTRA_ARGS: {exc}") from exc
    lock_dir = text("SLURM_LOCK_DIR")
    return LauncherSettings(
        mode=mode,
        jobid=jobid,
        job_name_regex=regex,
        partition=text("SLURM_PARTITION"),
        account=text("SLURM_ACCOUNT"),
        gres=gres,
        cpus=int(cpus_text) if cpus_text else None,
        mem=text("SLURM_MEM"),
        extra_args=extra_args,
        **({"lock_dir": Path(lock_dir)} if lock_dir else {}),
    )


def _squeue(arguments: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            ["squeue", *arguments], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LauncherError(f"cannot query Slurm: {exc.__class__.__name__}") from exc
    if completed.returncode != 0:
        raise LauncherError(f"squeue failed: {completed.stderr.strip()[:200]}")
    return completed.stdout


Squeue = Callable[[Sequence[str]], str]


def running_jobs(squeue: Squeue = _squeue) -> list[tuple[str, str, str]]:
    """The caller's running jobs as ``(job id, name, partition)``."""
    jobs = []
    for line in squeue(["--me", "-h", "-t", "R", "-o", "%i|%j|%P"]).splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0].isdigit():
            jobs.append((parts[0], parts[1], parts[2]))
    return jobs


def _srun_flags(settings: LauncherSettings) -> list[str]:
    flags = ["--export=ALL", "--kill-on-bad-exit=1"]
    if settings.gres:
        flags.append(f"--gres={settings.gres}")
    if settings.cpus:
        flags.append(f"--cpus-per-task={settings.cpus}")
    if settings.mem:
        flags.append(f"--mem={settings.mem}")
    return flags + list(settings.extra_args)


def _job_plan(command: Sequence[str], settings: LauncherSettings, jobid: str) -> LaunchPlan:
    srun = ["srun", f"--jobid={jobid}", "--overlap", *_srun_flags(settings), "--", *command]
    return LaunchPlan(
        srun,
        {"mode": "slurm-job", "jobid": jobid, "gres": settings.gres},
        drop_environment_prefix=JOB_ENV_PREFIX,
    )


def _alloc_plan(
    command: Sequence[str], settings: LauncherSettings, timeout_seconds: float
) -> LaunchPlan:
    minutes = int(timeout_seconds // 60) + 2
    srun = ["srun", "--job-name=agsci-eval", f"--time={minutes}", *_srun_flags(settings)]
    if settings.partition:
        srun.append(f"--partition={settings.partition}")
    if settings.account:
        srun.append(f"--account={settings.account}")
    srun += ["--", *command]
    return LaunchPlan(
        srun,
        {
            "mode": "slurm-alloc",
            "partition": settings.partition,
            "gres": settings.gres,
            "time_minutes": minutes,
        },
        drop_environment_prefix=JOB_ENV_PREFIX,
    )


def _lock_path(settings: LauncherSettings, jobid: str) -> Path:
    settings.lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return settings.lock_dir / f"job-{jobid}.lock"


@contextmanager
def _held_lock(path: Path, *, wait_seconds: float) -> Iterator[bool]:
    """Hold an exclusive advisory lock; yields False when it stayed busy for ``wait_seconds``."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + wait_seconds
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        yield acquired
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def planned_launch(
    command: Sequence[str],
    *,
    settings: LauncherSettings,
    timeout_seconds: float,
    squeue: Squeue = _squeue,
) -> Iterator[LaunchPlan]:
    """Yield how to run ``command``; any job it needs stays reserved until the block ends."""
    if settings.mode == "local":
        yield LaunchPlan(list(command), {"mode": "local"})
        return
    if settings.mode == "slurm-alloc":
        yield _alloc_plan(command, settings, timeout_seconds)
        return
    if settings.mode == "slurm-job":
        assert settings.jobid is not None
        if settings.jobid not in {job[0] for job in running_jobs(squeue)}:
            raise LauncherError(f"Slurm job {settings.jobid} is not one of your running jobs")
        with _held_lock(_lock_path(settings, settings.jobid), wait_seconds=timeout_seconds) as held:
            if not held:
                raise LauncherError(f"Slurm job {settings.jobid} stayed busy for {timeout_seconds:.0f}s")
            yield _job_plan(command, settings, settings.jobid)
        return
    pattern = re.compile(settings.job_name_regex)
    for jobid, name, partition in running_jobs(squeue):
        if not pattern.search(name) or (settings.partition and partition != settings.partition):
            continue
        with _held_lock(_lock_path(settings, jobid), wait_seconds=0.0) as held:
            if held:
                yield _job_plan(command, settings, jobid)
                return
    yield _alloc_plan(command, settings, timeout_seconds)


def launch_environment(environment: Mapping[str, str], plan: LaunchPlan) -> dict[str, str]:
    """The environment for the launcher process: ``SLURM_*`` of an enclosing job would make
    ``srun`` run inside that job instead of the one chosen."""
    prefix = plan.drop_environment_prefix
    return {
        key: value for key, value in environment.items() if not (prefix and key.startswith(prefix))
    }
