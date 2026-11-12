"""Validate and atomically back up SQLite, including committed WAL pages."""

from pathlib import Path
from contextlib import closing
import os
import sqlite3
import sys
import tempfile


def snapshot(source: Path, destination: Path) -> None:
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Missing or empty history database")
    if source.resolve() == destination.resolve():
        raise ValueError("Snapshot source and destination must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".history-", suffix=".sqlite", dir=destination.parent)
    os.close(handle)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            if src.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("SQLite history integrity check failed")
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("SQLite snapshot integrity check failed")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    try:
        snapshot(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"History snapshot failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
