import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
import pytest

from attestory import engine
from attestory.engine import commit_file, sha256_file


def _write_csv(path: Path, amount: int) -> None:
    path.write_text(f"Revenue\n{amount}\n", encoding="utf-8")


def test_interrupted_history_publish_keeps_previous_history_readable(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "actuals.csv"
    _write_csv(source, 250)
    baseline = commit_file(tmp_path, source)
    history_path = tmp_path / ".attestory" / "history.json"
    original_history = history_path.read_bytes()
    baseline_snapshot = tmp_path / baseline["record"]["snapshot"]

    _write_csv(source, 500)
    original_replace = engine.os.replace

    def interrupt_history_replace(source_path, target_path):
        if Path(target_path) == history_path:
            raise OSError("simulated interruption")
        original_replace(source_path, target_path)

    monkeypatch.setattr(engine.os, "replace", interrupt_history_replace)

    with pytest.raises(OSError, match="simulated interruption"):
        commit_file(tmp_path, source)

    assert history_path.read_bytes() == original_history
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert [record["version"] for record in history["commits"]] == [1]
    assert sha256_file(baseline_snapshot) == baseline["record"]["snapshot_sha256"]
    snapshots = sorted((tmp_path / ".attestory" / "snapshots").glob("*.parquet"))
    assert len(snapshots) == 2
    unreferenced_snapshot = next(
        path for path in snapshots if path != baseline_snapshot
    )
    engine._verify_parquet_snapshot(unreferenced_snapshot)


def test_invalid_staged_snapshot_is_not_published(tmp_path: Path, monkeypatch):
    source = tmp_path / "actuals.csv"
    _write_csv(source, 250)
    baseline = commit_file(tmp_path, source)
    history_path = tmp_path / ".attestory" / "history.json"
    original_history = history_path.read_bytes()
    _write_csv(source, 500)

    def write_truncated_snapshot(_source_path: Path, snapshot_path: Path):
        snapshot_path.write_bytes(b"partial parquet")
        return {"rows": 1, "columns": 1, "metrics": 1}

    monkeypatch.setattr(engine, "extract_csv_to_parquet", write_truncated_snapshot)

    with pytest.raises(RuntimeError, match="not a complete Parquet"):
        commit_file(tmp_path, source)

    assert history_path.read_bytes() == original_history
    assert (
        sha256_file(tmp_path / baseline["record"]["snapshot"])
        == (baseline["record"]["snapshot_sha256"])
    )
    snapshots = tmp_path / ".attestory" / "snapshots"
    assert len(list(snapshots.glob("*.parquet"))) == 1
    assert not list(snapshots.glob(".attestory-stage-*.parquet"))


def test_source_change_during_snapshot_is_reported_as_conflict(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "actuals.csv"
    _write_csv(source, 250)
    history_path = tmp_path / ".attestory" / "history.json"
    original_extractor = engine.extract_csv_to_parquet

    def modify_source_during_extraction(source_path: Path, snapshot_path: Path):
        stats = original_extractor(source_path, snapshot_path)
        _write_csv(source_path, 500)
        return stats

    monkeypatch.setattr(
        engine, "extract_csv_to_parquet", modify_source_during_extraction
    )

    with pytest.raises(RuntimeError, match="Artifact changed while creating"):
        commit_file(tmp_path, source)

    assert json.loads(history_path.read_text(encoding="utf-8"))["commits"] == []
    assert not list((tmp_path / ".attestory" / "snapshots").glob("*.parquet"))


def test_concurrent_commits_preserve_history_and_snapshot_digests(tmp_path: Path):
    sources = [tmp_path / "actuals.csv", tmp_path / "budget.csv"]
    _write_csv(sources[0], 250)
    _write_csv(sources[1], 900)
    barrier = threading.Barrier(3)

    def commit_after_barrier(source: Path):
        barrier.wait()
        return commit_file(tmp_path, source)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(commit_after_barrier, source)
            for source in (sources[0], sources[1], sources[0])
        ]
        results = [future.result() for future in futures]

    history = json.loads(
        (tmp_path / ".attestory" / "history.json").read_text(encoding="utf-8")
    )
    assert len(history["commits"]) == 3
    assert sorted(
        record["version"]
        for record in history["commits"]
        if record["artifact_key"] == "actuals.csv"
    ) == [1, 2]
    assert [
        record["artifact_key"]
        for record in history["commits"]
        if record["artifact_key"] == "budget.csv"
    ] == ["budget.csv"]

    for result in results:
        record = result["record"]
        snapshot_path = tmp_path / record["snapshot"]
        assert snapshot_path.is_file()
        assert sha256_file(snapshot_path) == record["snapshot_sha256"]
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                "SELECT COUNT(*) FROM read_parquet(?)", [str(snapshot_path)]
            ).fetchone()
        finally:
            connection.close()
