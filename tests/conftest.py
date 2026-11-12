"""Use this checkout without requiring an install or packaging metadata."""

from pathlib import Path
import getpass
import os
import re
import sys
import uuid

import pytest

REPO_ROOT = str(Path(__file__).resolve().parents[1])
if REPO_ROOT in sys.path:
    sys.path.remove(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # The managed Windows host can deny access to its shared pytest temp root.
    # Keep each invocation isolated and writable, honoring an explicit --basetemp.
    username = re.sub(r"[^A-Za-z0-9_.-]", "_", os.environ.get("USERNAME") or getpass.getuser())
    # A sandbox can inherit the real user's USERNAME. Include the process login
    # as well, so those Windows accounts never share a cache directory.
    try:
        login = re.sub(r"[^A-Za-z0-9_.-]", "_", os.getlogin())
    except OSError:
        login = username
    cache_user = username if login == username else username + "-" + login
    config._inicache["cache_dir"] = str(Path(REPO_ROOT) / "runs" / "pytest" / (cache_user + "-cache"))
    if config.option.basetemp is None:
        temp_parent = Path(REPO_ROOT) / "runs" / "pytest"
        temp_parent.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(temp_parent / (username + "-" + uuid.uuid4().hex))
