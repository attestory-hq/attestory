"""Debounced filesystem watcher for automatic Attestory snapshots."""

import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from attestory.engine import (
    ATTESTORY_DIR,
    artifact_key,
    commit_file,
    commits_for_artifact,
    sha256_file,
)

SUPPORTED_EXTENSIONS = {".xlsx", ".pptx", ".html", ".csv"}
IGNORED_DIRECTORIES = {".attestory", ".git", ".venv", "__pycache__"}


def is_ignored_path(path: Path) -> bool:
    """Return whether a file path should be excluded from watcher processing."""
    if path.name.startswith("~$") or path.suffix.lower() == ".tmp":
        return True
    if any(part in IGNORED_DIRECTORIES for part in path.parts):
        return True
    return path.suffix.lower() not in SUPPORTED_EXTENSIONS


def snapshot_if_changed(
    root: Path,
    path: Path,
    retry_attempts: int = 3,
    retry_delay: float = 0.1,
) -> dict | None:
    """Snapshot a supported file unless its content matches the latest commit."""
    root = root.resolve()
    path = path.resolve()
    if is_ignored_path(path) or not path.is_file():
        return None

    last_error: Exception | None = None
    attempts = max(1, retry_attempts)
    for attempt in range(attempts):
        try:
            manifest_path = root / ATTESTORY_DIR / "history.json"
            if manifest_path.exists():
                history = json.loads(manifest_path.read_text(encoding="utf-8"))
                commits = commits_for_artifact(
                    history, artifact_key(root, path), include_legacy=False
                )
                if commits and commits[-1]["sha256"] == sha256_file(path):
                    return None

            return commit_file(root, path, message="Auto-snapshot on file save")
        except Exception as error:  # noqa: BLE001 - async read failures are reported after bounded retries
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(retry_delay * (2**attempt))

    if last_error is not None:
        raise last_error
    return None


class SnapshotEventHandler(FileSystemEventHandler):
    """Debounce write events and snapshot each changed file once it settles."""

    def __init__(
        self,
        root: Path,
        debounce: float = 2.0,
        on_commit: Callable[[dict], None] | None = None,
        on_error: Callable[[Path, Exception], None] | None = None,
        retry_attempts: int = 3,
        retry_delay: float = 0.1,
    ):
        super().__init__()
        self.root = root.resolve()
        self.debounce = debounce
        self.on_commit = on_commit or self._print_receipt
        self.on_error = on_error or self._print_error
        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay
        self._timers: dict[Path, threading.Timer] = {}
        self._tokens: dict[Path, int] = {}
        self._lock = threading.Lock()
        self._processing_lock = threading.Lock()
        self._closed = False

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle_event(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle_event(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        destination = getattr(event, "dest_path", None)
        if destination is None:
            return
        path = Path(destination).resolve()
        if not path.is_relative_to(self.root):
            return
        if is_ignored_path(path):
            return
        self._schedule(path)

    def _handle_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if is_ignored_path(path):
            return
        self._schedule(path.resolve())

    def _schedule(self, path: Path) -> None:
        with self._lock:
            previous = self._timers.pop(path, None)
            if previous:
                previous.cancel()
            token = self._tokens.get(path, 0) + 1
            self._tokens[path] = token
            timer = threading.Timer(
                self.debounce, self._snapshot_after_debounce, args=(path, token)
            )
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def _snapshot_after_debounce(self, path: Path, token: int) -> None:
        with self._lock:
            if self._closed or self._tokens.get(path) != token:
                return
            self._timers.pop(path, None)
        try:
            with self._processing_lock:
                with self._lock:
                    if self._closed:
                        return
                result = snapshot_if_changed(
                    self.root,
                    path,
                    retry_attempts=self.retry_attempts,
                    retry_delay=self.retry_delay,
                )
            if result is not None:
                self.on_commit(result)
        except Exception as error:  # noqa: BLE001 - async failures must reach on_error
            self.on_error(path, error)

    def stop(self) -> None:
        """Cancel pending debounced saves during watcher shutdown."""
        with self._lock:
            self._closed = True
            timers = list(self._timers.values())
            self._timers.clear()
            self._tokens.clear()
        for timer in timers:
            timer.cancel()
        with self._processing_lock:
            pass

    @staticmethod
    def _print_receipt(result: dict) -> None:
        record = result["record"]
        print(
            f"Auto-snapshot {record['file']} v{record['version']} "
            f"— SHA-256 {record['sha256'][:12]}"
        )

    @staticmethod
    def _print_error(path: Path, error: Exception) -> None:
        print(f"Could not snapshot {path}: {error}", file=sys.stderr)


def run_watcher(
    root: Path,
    recursive: bool = False,
    debounce: float = 2.0,
    on_commit: Callable[[dict], None] | None = None,
    on_error: Callable[[Path, Exception], None] | None = None,
    retry_attempts: int = 3,
    retry_delay: float = 0.1,
) -> None:
    """Watch a workspace until interrupted, debouncing saves before snapshotting."""
    handler = SnapshotEventHandler(
        root,
        debounce=debounce,
        on_commit=on_commit,
        on_error=on_error,
        retry_attempts=retry_attempts,
        retry_delay=retry_delay,
    )
    observer = Observer()
    observer.schedule(handler, str(root.resolve()), recursive=recursive)
    observer.start()
    try:
        observer.join()
    finally:
        observer.stop()
        observer.join()
        handler.stop()
