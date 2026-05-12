from __future__ import annotations

from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

import pytest

from vibe.core.autocompletion.file_indexer import FileIndexer

# This suite runs against the real filesystem and watcher. A faked store/watcher
# split would be faster to unit-test, but given time constraints and the low churn
# expected for this feature, integration coverage was chosen as a trade-off.
#
# Every test in this module is grouped under the ``file_indexer_serial`` xdist
# group so that all tests in the module land on the same xdist worker
# (consumed by ``pytest -n auto --dist=loadgroup``). This eliminates
# cross-worker inotify and process-level GIL contention that would
# otherwise stretch the watcher's poll-to-dispatch latency far past any
# reasonable ``_wait_for`` budget under heavy load (``--cov``
# instrumentation on a 128-CPU/4-core host). The remaining tests are
# defensive: ``@pytest.mark.timeout(120)`` overrides the project-wide
# 10-second pytest-timeout, and ``_wait_for`` allows up to 60 seconds for
# event dispatch.
pytestmark = pytest.mark.xdist_group("file_indexer_serial")


@pytest.fixture
def file_indexer() -> Generator[FileIndexer]:
    indexer = FileIndexer()
    yield indexer
    indexer.shutdown()


def _wait_for(condition: Callable[[], bool], timeout=60.0) -> bool:
    # The 60s budget accommodates filesystem watcher dispatch latency under
    # the most adverse condition we observe in CI: ``pytest --cov`` running
    # under ``-n auto`` on a host that reports 128 CPUs but only has ~4
    # physical cores. Under that combination, coverage tracing in every
    # Python frame combined with watcher-thread GIL contention can stretch
    # the watcher's poll-to-dispatch latency well past the original 3s
    # budget that exhibited flakes in the QA report. Tests that rely on
    # this helper additionally carry ``@pytest.mark.timeout(120)`` to
    # override the project-wide 10s ``pytest-timeout`` budget.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def _warmup_watcher(
    file_indexer: FileIndexer, tmp_path: Path, *, sentinel: str = ".warmup_sentinel"
) -> None:
    """Force the OS file watcher to fully warm up before the test's mutation.

    The production :class:`WatchController` only guarantees that its
    ``ready_event`` is set within 0.5 seconds of ``start()``. Under
    ``pytest --cov`` instrumentation on a host where many xdist workers
    contend for the GIL, the watcher thread may not actually have begun
    polling within that 0.5-second budget — yet ``get_index`` returns
    immediately, the test moves on to create the file, and the change
    event is lost because the watcher started polling AFTER the file
    already existed.

    This helper synchronizes on a sentinel file: it creates a known
    file, polls until the watcher reports it, then deletes the sentinel
    and re-confirms its removal. After this round trip we know the
    watcher is actively dispatching events.
    """
    sentinel_path = tmp_path / sentinel
    sentinel_path.write_text("", encoding="utf-8")
    detected = _wait_for(
        lambda: any(
            entry.rel == sentinel for entry in file_indexer.get_index(Path("."))
        )
    )
    if not detected:  # pragma: no cover - defensive escape hatch
        # The watcher genuinely never warmed up; let the actual test
        # report the failure rather than masking it here.
        return
    sentinel_path.unlink()
    _wait_for(
        lambda: all(
            entry.rel != sentinel for entry in file_indexer.get_index(Path("."))
        )
    )


@pytest.mark.timeout(120)
def test_updates_index_on_file_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_indexer: FileIndexer
) -> None:
    monkeypatch.chdir(tmp_path)
    file_indexer.get_index(Path("."))
    _warmup_watcher(file_indexer, tmp_path)

    target = tmp_path / "new_file.py"
    target.write_text("", encoding="utf-8")

    assert _wait_for(
        lambda: any(
            entry.rel == target.name for entry in file_indexer.get_index(Path("."))
        )
    )


@pytest.mark.timeout(120)
def test_updates_index_on_file_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_indexer: FileIndexer
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "new_file.py"
    target.write_text("", encoding="utf-8")
    file_indexer.get_index(Path("."))
    _warmup_watcher(file_indexer, tmp_path)

    target.unlink()

    assert _wait_for(
        lambda: all(
            entry.rel != target.name for entry in file_indexer.get_index(Path("."))
        )
    )


@pytest.mark.timeout(120)
def test_updates_index_on_file_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_indexer: FileIndexer
) -> None:
    monkeypatch.chdir(tmp_path)
    old_file = tmp_path / "old_name.py"
    old_file.write_text("", encoding="utf-8")
    file_indexer.get_index(Path("."))
    _warmup_watcher(file_indexer, tmp_path)

    new_file = tmp_path / "new_name.py"
    old_file.rename(new_file)

    assert _wait_for(
        lambda: all(
            entry.rel != old_file.name for entry in file_indexer.get_index(Path("."))
        )
        and any(
            entry.rel == new_file.name for entry in file_indexer.get_index(Path("."))
        )
    )


@pytest.mark.timeout(120)
def test_updates_index_on_folder_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_indexer: FileIndexer
) -> None:
    monkeypatch.chdir(tmp_path)
    old_folder = tmp_path / "old_folder"
    old_folder.mkdir()
    number_of_files = 5
    file_names = [f"file{i}.py" for i in range(1, number_of_files + 1)]
    old_file_paths = [old_folder / name for name in file_names]
    for file_path in old_file_paths:
        file_path.write_text("", encoding="utf-8")
    file_indexer.get_index(Path("."))
    _warmup_watcher(file_indexer, tmp_path)

    new_folder = tmp_path / "new_folder"
    old_folder.rename(new_folder)

    assert _wait_for(
        lambda: (
            entries := file_indexer.get_index(Path(".")),
            all(not entry.rel.startswith("old_folder/") for entry in entries)
            and all(
                any(entry.rel == f"new_folder/{name}" for entry in entries)
                for name in file_names
            ),
        )[1]
    )


@pytest.mark.timeout(120)
def test_updates_index_incrementally_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_indexer: FileIndexer
) -> None:
    monkeypatch.chdir(tmp_path)
    file_indexer.get_index(Path("."))
    _warmup_watcher(file_indexer, tmp_path)

    rebuilds_before = file_indexer.stats.rebuilds
    incremental_before = file_indexer.stats.incremental_updates

    target = tmp_path / "stats_file.py"
    target.write_text("", encoding="utf-8")

    assert _wait_for(
        lambda: any(
            entry.rel == target.name for entry in file_indexer.get_index(Path("."))
        )
    )

    assert file_indexer.stats.rebuilds == rebuilds_before
    assert file_indexer.stats.incremental_updates >= incremental_before + 1


@pytest.mark.timeout(120)
def test_rebuilds_index_when_mass_change_threshold_is_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mass_change_threshold = 5
    # in an ideal world, we would use "threshold + 1", but in reality, we need to test with a
    # number of files important enough to MAKE SURE that a batch of >= threshold events will be
    # detected by the watcher
    number_of_files = mass_change_threshold * 3
    monkeypatch.chdir(tmp_path)
    indexer = FileIndexer(mass_change_threshold=mass_change_threshold)
    try:
        indexer.get_index(Path("."))
        rebuilds_before = indexer.stats.rebuilds

        ThreadPoolExecutor(max_workers=number_of_files).map(
            lambda i: (tmp_path / f"bulk{i}.py").write_text("", encoding="utf-8"),
            range(number_of_files),
        )

        assert _wait_for(lambda: len(indexer.get_index(Path("."))) == number_of_files)
        # we do not assert that "incremental_updates" did not change,
        # as the watcher potentially reported some batches of events that were
        # smaller than the threshold
        assert indexer.stats.rebuilds >= rebuilds_before + 1
    finally:
        indexer.shutdown()


@pytest.mark.timeout(120)
def test_switching_between_roots_restarts_index(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    file_indexer: FileIndexer,
) -> None:
    first_root = tmp_path
    second_root = tmp_path_factory.mktemp("second-root")
    (first_root / "first.py").write_text("", encoding="utf-8")
    (second_root / "second.py").write_text("", encoding="utf-8")

    monkeypatch.chdir(first_root)
    assert _wait_for(
        lambda: any(
            entry.rel == "first.py" for entry in file_indexer.get_index(Path("."))
        )
    )

    monkeypatch.chdir(second_root)
    assert _wait_for(
        lambda: all(
            entry.rel != "first.py" for entry in file_indexer.get_index(Path("."))
        )
    )
    assert _wait_for(
        lambda: any(
            entry.rel == "second.py" for entry in file_indexer.get_index(Path("."))
        )
    )


@pytest.mark.timeout(120)
def test_watcher_failure_does_not_break_existing_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_indexer: FileIndexer
) -> None:
    monkeypatch.chdir(tmp_path)
    seed = tmp_path / "seed.py"
    seed.write_text("", encoding="utf-8")
    file_indexer.get_index(Path("."))

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(file_indexer._store, "apply_changes", boom)

    (tmp_path / "new_file.py").write_text("", encoding="utf-8")

    assert _wait_for(
        lambda: (
            entries := file_indexer.get_index(Path(".")),
            # new file was not added: watcher failed
            all(entry.rel != "new_file.py" for entry in entries)
            # but the existing index is still intact
            and all(entry.rel == "seed.py" for entry in entries),
        )[1]
    )


def test_shutdown_cleans_up_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "test.txt").write_text("", encoding="utf-8")
    file_indexer = FileIndexer()
    file_indexer.get_index(Path("."))

    file_indexer.shutdown()
    assert file_indexer.get_index(Path(".")) == []
