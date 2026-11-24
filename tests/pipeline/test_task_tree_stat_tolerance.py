"""The task-tree copy check tolerates a 1 ns ctime flap but still catches real changes."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.pipeline.safe_fs import UnsafeGluePathError
from src.pipeline.task_tree import _same_entry, copy_task_tree_at


def _stat(**changes: int) -> os.stat_result:
    values = dict(st_dev=1, st_ino=2, st_mode=0o100644, st_size=10, st_mtime_ns=1_000_000, st_ctime_ns=2_000_000)
    values.update(changes)
    return os.stat_result(
        (values['st_mode'], values['st_ino'], values['st_dev'], 1, 0, 0, values['st_size'], 0, 0, 0),
        {'st_mtime_ns': values['st_mtime_ns'], 'st_ctime_ns': values['st_ctime_ns']},
    )


def test_one_nanosecond_ctime_flap_is_not_a_change() -> None:
    assert _same_entry(_stat(), _stat(st_ctime_ns=2_000_001))


@pytest.mark.parametrize(
    "changes",
    [
        {"st_ctime_ns": 2_000_000 + 2_000},
        {"st_mtime_ns": 1_000_001},
        {"st_size": 11},
        {"st_ino": 3},
        {"st_mode": 0o100600},
    ],
)
def test_real_changes_are_still_detected(changes: dict[str, int]) -> None:
    assert not _same_entry(_stat(), _stat(**changes))


def test_copy_still_rejects_a_file_changed_while_copying(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    destination.mkdir()
    (source / "a.txt").write_text("one")
    real_copyfileobj = __import__("shutil").copyfileobj

    def mutate_then_copy(reader, writer, *args, **kwargs):
        real_copyfileobj(reader, writer, *args, **kwargs)
        (source / "a.txt").write_text("changed while copying")

    monkeypatch.setattr("src.pipeline.task_tree.shutil.copyfileobj", mutate_then_copy)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    destination_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(UnsafeGluePathError, match="changed while copying"):
            copy_task_tree_at(source_fd, destination_fd, relative_dir=Path(), ignore_patterns=())
    finally:
        os.close(source_fd)
        os.close(destination_fd)
