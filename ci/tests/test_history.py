"""Offline integration tests: every Git remote is a disposable local bare repo.

Run with: python -m unittest discover -s ci/tests -v
Requires Git, Bash and Python 3.12; no bats, pytest, APIs or credentials.
"""

from contextlib import closing
import os
from pathlib import Path
import shutil
import shlex
import sqlite3
import subprocess
import tempfile
import unittest

from ci.sqlite_snapshot import snapshot


CI = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
GIT = shutil.which("git")


@unittest.skipUnless(BASH and GIT, "Git and Bash are required")
class HistoryTests(unittest.TestCase):
    def setUp(self):
        runtime = CI / "runtime"
        runtime.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="history-tests-", dir=runtime)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                    "GIT_AUTHOR_NAME": "History Test", "GIT_COMMITTER_NAME": "History Test",
                    "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "commit.gpgsign",
                    "GIT_CONFIG_VALUE_0": "false"}
        self.git(self.root, "init", "--bare", "--template=", str(self.remote))
        self.work = self.new_checkout("work")

    def git(self, cwd, *args, ok=True):
        proc = subprocess.run([GIT, *args], cwd=cwd, env=self.env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def new_checkout(self, name):
        path = self.root / name
        path.mkdir()
        self.git(path, "init", "--template=", "--initial-branch=main")
        self.git(path, "remote", "add", "origin", str(self.remote))
        (path / "source.txt").write_text("source stays on main\n", encoding="utf-8")
        self.git(path, "add", "source.txt")
        self.git(path, "commit", "-m", "Source commit")
        return path

    def script(self, name, cwd=None, ok=True):
        proc = subprocess.run([BASH, (CI / name).as_posix()], cwd=cwd or self.work,
                              env=self.env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def add_observation(self, day, cwd=None):
        db = (cwd or self.work) / "data/gpu_prices.sqlite"
        db.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS offers(day TEXT PRIMARY KEY, price REAL)")
            conn.execute("INSERT INTO offers VALUES (?, 3.5)", (day,))
        return db

    def seed(self):
        self.script("restore_db.sh")
        self.add_observation("2026-11-10")
        self.script("save_db.sh")

    def rows(self, cwd):
        with closing(sqlite3.connect(cwd / "data/gpu_prices.sqlite")) as conn:
            return conn.execute("SELECT day FROM offers ORDER BY day").fetchall()

    def remote_commit(self):
        return self.git(self.remote, "rev-parse", "refs/heads/data").stdout.strip()

    def test_first_save_is_orphan_and_only_contains_history_files(self):
        before = self.git(self.work, "status", "--porcelain", "--untracked-files=no").stdout
        head = self.git(self.work, "rev-parse", "HEAD").stdout
        self.seed()
        self.assertEqual(self.git(self.remote, "ls-tree", "--name-only", "data").stdout.splitlines(),
                         ["README.md", "gpu_prices.sqlite"])
        self.assertEqual(len(self.git(self.remote, "rev-list", "--parents", "-n", "1", "data").stdout.split()), 1)
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD").stdout, head)
        self.assertEqual(self.git(self.work, "status", "--porcelain", "--untracked-files=no").stdout, before)

    def test_restore_append_save_preserves_old_days(self):
        self.seed()
        second = self.new_checkout("second")
        self.script("restore_db.sh", second)
        self.add_observation("2026-11-11", second)
        self.script("save_db.sh", second)
        third = self.new_checkout("third")
        self.script("restore_db.sh", third)
        self.assertEqual(self.rows(third), [("2026-11-10",), ("2026-11-11",)])

    def test_missing_branch_can_be_seeded_with_existing_history(self):
        self.add_observation("2026-11-01")
        self.script("restore_db.sh")
        self.script("save_db.sh")
        other = self.new_checkout("seed-check")
        self.script("restore_db.sh", other)
        self.assertEqual(self.rows(other), [("2026-11-01",)])

    def test_unreachable_remote_is_not_treated_as_first_run(self):
        self.git(self.work, "remote", "set-url", "origin", str(self.root / "does-not-exist"))
        self.script("restore_db.sh", ok=False)
        self.assertFalse((self.work / "ci/runtime/history-base").exists())
        self.add_observation("2026-11-10")
        self.script("save_db.sh", ok=False)

    def test_restore_refuses_to_overwrite_existing_local_history(self):
        self.seed()
        self.script("restore_db.sh", ok=False)
        self.assertEqual(self.rows(self.work), [("2026-11-10",)])
        self.assertFalse((self.work / "ci/runtime/history-base").exists())

    def test_corrupt_database_never_replaces_remote_history(self):
        self.seed()
        before = self.remote_commit()
        (self.work / "data/gpu_prices.sqlite").write_bytes(b"not a database")
        self.script("save_db.sh", ok=False)
        self.assertEqual(self.remote_commit(), before)

    def test_concurrent_database_update_is_refused_without_lost_history(self):
        self.seed()
        second = self.new_checkout("second")
        self.script("restore_db.sh", second)
        self.add_observation("2026-11-11", second)
        self.script("save_db.sh", second)
        before = self.remote_commit()
        self.add_observation("2026-11-12")
        proc = self.script("save_db.sh", ok=False)
        self.assertIn("Remote history changed", proc.stderr)
        self.assertEqual(self.remote_commit(), before)
        self.assertTrue((self.work / "ci/runtime/history-snapshot.sqlite").is_file())

    def test_metadata_only_update_is_retained_and_parent_is_latest_remote(self):
        self.seed()
        writer = self.root / "writer"
        self.git(self.root, "clone", "--branch", "data", str(self.remote), str(writer))
        (writer / "README.md").write_text("Updated history instructions\n", encoding="utf-8")
        self.git(writer, "add", "README.md")
        self.git(writer, "commit", "-m", "Update README")
        self.git(writer, "push", "origin", "data")
        before = self.remote_commit()
        self.add_observation("2026-11-11")
        self.script("save_db.sh")
        self.assertEqual(self.git(self.remote, "rev-parse", "data^").stdout.strip(), before)
        self.assertEqual(self.git(self.remote, "show", "data:README.md").stdout,
                         "Updated history instructions\n")

    def test_push_rejection_is_retried(self):
        self.seed()
        hook = self.remote / "hooks/pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text('#!/bin/sh\nif [ ! -f retry-marker ]; then\n  touch retry-marker\n  exit 1\nfi\n', encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        self.add_observation("2026-11-11")
        proc = self.script("save_db.sh")
        self.assertIn("push attempt 1 failed", proc.stderr)
        self.assertTrue((self.remote / "retry-marker").exists())
        other = self.new_checkout("retry-check")
        self.script("restore_db.sh", other)
        self.assertEqual(self.rows(other), [("2026-11-10",), ("2026-11-11",)])

    def test_real_push_race_retries_on_latest_parent(self):
        self.seed()
        writer = self.root / "racing-writer"
        self.git(self.root, "clone", "--branch", "data", str(self.remote), str(writer))
        hook = self.work / ".git/hooks/pre-push"
        hook.parent.mkdir(exist_ok=True)
        # Advance the remote after save fetched it, but before its push is sent.
        # Clear save's private index so the independent writer uses its own.
        hook.write_text(
            "#!/bin/sh\nset -eu\n"
            f"writer={shlex.quote(writer.as_posix())}\n"
            'if [ ! -f "$writer/race-marker" ]; then\n'
            '  touch "$writer/race-marker"\n'
            '  unset GIT_INDEX_FILE GIT_DIR GIT_WORK_TREE\n'
            '  printf "Concurrent metadata update\\n" >"$writer/README.md"\n'
            '  git -C "$writer" add README.md\n'
            '  git -C "$writer" commit -m "Concurrent README update"\n'
            '  git -C "$writer" push origin data\n'
            'fi\n', encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        self.add_observation("2026-11-11")
        proc = self.script("save_db.sh")
        self.assertIn("push attempt 1 failed", proc.stderr)
        self.assertEqual(self.git(self.remote, "rev-parse", "data^").stdout.strip(),
                         self.git(writer, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual(self.git(self.remote, "show", "data:README.md").stdout,
                         "Concurrent metadata update\n")

    def test_persistent_push_rejection_fails_and_retains_snapshot(self):
        self.seed()
        before = self.remote_commit()
        hook = self.remote / "hooks/pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text('#!/bin/sh\nexit 1\n', encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        self.add_observation("2026-11-11")
        proc = self.script("save_db.sh", ok=False)
        self.assertIn("after 3 attempts", proc.stderr)
        self.assertEqual(self.remote_commit(), before)
        self.assertTrue((self.work / "ci/runtime/history-snapshot.sqlite").is_file())

    def test_unexpected_files_on_data_branch_are_refused(self):
        self.seed()
        writer = self.root / "writer"
        self.git(self.root, "clone", "--branch", "data", str(self.remote), str(writer))
        (writer / "extra.txt").write_text("unrelated", encoding="utf-8")
        self.git(writer, "add", "extra.txt")
        self.git(writer, "commit", "-m", "Unexpected file")
        self.git(writer, "push", "origin", "data")
        other = self.new_checkout("invalid-tree")
        self.script("restore_db.sh", other, ok=False)
        self.assertFalse((other / "data/gpu_prices.sqlite").exists())

    def test_snapshot_keeps_committed_wal_rows(self):
        db = self.add_observation("2026-11-10")
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("INSERT INTO offers VALUES ('2026-11-11', 4)")
            conn.commit()
            self.assertTrue(Path(str(db) + "-wal").is_file())
            target = self.root / "wal-snapshot.sqlite"
            snapshot(db, target)
            with closing(sqlite3.connect(target)) as check:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM offers").fetchone()[0], 2)
