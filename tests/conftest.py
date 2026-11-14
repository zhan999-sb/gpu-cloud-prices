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
    if os.name == "nt":
        # CPython's Windows 0700 ACL prevents the restricted test token from
        # reopening its own scratch directories. Use inherited ACLs, including
        # unittest TemporaryDirectory, and restore the process API at exit.
        mkdir = os.mkdir

        def inherited_mkdir(path, mode=0o777, *, dir_fd=None):
            return mkdir(path, 0o777 if mode == 0o700 else mode, dir_fd=dir_fd)

        os.mkdir = inherited_mkdir
        config.add_cleanup(lambda: setattr(os, "mkdir", mkdir))

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


@pytest.fixture(scope="session")
def gpu_text_files(request):
    """Reuse known UTF-8 contents of versioned files in this run's scratch only.

    Real writes/copies still happen. Observe writes and atomic moves, check file
    identity/size/timestamps on every hit, and bypass open writers or modified
    IO hooks. Generated corruption, removal and temporary catalogs remain live.
    """
    import builtins
    from collections import OrderedDict
    import io
    import shutil
    import stat
    import weakref

    base = Path(os.path.abspath(request.config.option.basetemp))
    entries = OrderedDict()
    writers = weakref.WeakSet()
    cached_chars = 0
    counts = dict(hits=0, misses=0, seeded_writes=0, seeded_copies=0)
    real_read, real_write = Path.read_text, Path.write_text
    real_open, path_open = io.open, Path.open
    real_copy, real_copy2 = shutil.copyfile, shutil.copy2
    real_replace, real_rename = Path.replace, Path.rename
    missing = object()

    def eligible(path):
        if not isinstance(path, (str, os.PathLike)):
            return None
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(base):
            return None
        if any(p.startswith('.pw-profile') or p in {'.env', 'vault'} for p in path.parts):
            return None
        name = path.name.removesuffix('.tmp')
        if Path(name).suffix not in {'.html', '.json', '.xml', '.svg', '.css', '.txt'} and name != '_headers':
            return None
        return path

    def version(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
            return None
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def discard(path):
        nonlocal cached_chars
        previous = entries.pop(path, None)
        if previous is not None:
            cached_chars -= len(previous[1])

    def intact():
        return (io.open is tracked_open and builtins.open is tracked_open
                and Path.open is path_open and not any(not stream.closed for stream in writers))

    def remember(path, text):
        nonlocal cached_chars
        if path is None or not intact():
            return
        try:
            stamp = version(path)
        except OSError:
            return
        if stamp is None:
            return
        discard(path)
        entries[path] = (stamp, text)
        cached_chars += len(text)
        while len(entries) > 4096 or cached_chars > 64_000_000:
            _, (_, removed) = entries.popitem(last=False)
            cached_chars -= len(removed)

    def known(path):
        if path is None or not intact():
            return missing
        item = entries.get(path)
        if item is None:
            return missing
        try:
            stamp = version(path)
        except OSError:
            discard(path)
            return missing
        if stamp is None or item[0] != stamp:
            discard(path)
            return missing
        entries.move_to_end(path)
        return item[1]

    def tracked_open(file, *args, **kwargs):
        mode = kwargs.get('mode', args[0] if args else 'r')
        path = eligible(file)
        writing = path is not None and isinstance(mode, str) and any(c in mode for c in 'wax+')
        if writing:
            discard(path)
        stream = real_open(file, *args, **kwargs)
        if writing:
            writers.add(stream)
        return stream

    def read_text(path, *args, **kwargs):
        key = eligible(path)
        utf8 = (not args and set(kwargs) <= {'encoding', 'errors'}
                and kwargs.get('encoding') == 'utf-8' and kwargs.get('errors') in (None, 'strict'))
        if key is not None and utf8:
            content = known(key)
            if content is not missing:
                counts['hits'] += 1
                return content
            counts['misses'] += 1
        result = real_read(path, *args, **kwargs)
        if key is not None and utf8:
            remember(key, result)
        return result

    def write_text(path, data, *args, **kwargs):
        key = eligible(path)
        discard(key)
        result = real_write(path, data, *args, **kwargs)
        if (key is not None and not args and kwargs.get('encoding') == 'utf-8'
                and kwargs.get('errors') in (None, 'strict') and Path.write_text is write_text):
            newline = kwargs.get('newline')
            output = data.replace('\n', os.linesep if newline is None else newline) if newline != '' else data
            remember(key, output.replace('\r\n', '\n').replace('\r', '\n'))
            counts['seeded_writes'] += 1
        return result

    def copyfile(src, dst, *args, **kwargs):
        source, target = eligible(src), eligible(dst)
        content = known(source)
        discard(target)
        result = real_copy(src, dst, *args, **kwargs)
        if content is not missing:
            remember(target, content)
            counts['seeded_copies'] += 1
        return result

    def copy2(src, dst, *args, **kwargs):
        source, target = eligible(src), eligible(dst)
        content = known(source)
        discard(target)
        # Keep CopyFile2 on Windows; copyfile would fall back to Python IO.
        result = real_copy2(src, dst, *args, **kwargs)
        if content is not missing:
            remember(target, content)
            counts['seeded_copies'] += 1
        return result

    def move(path, target, operation):
        source, destination = eligible(path), eligible(target)
        content = known(source)
        discard(source)
        discard(destination)
        result = operation(path, target)
        if content is not missing:
            remember(destination, content)
        return result

    def replace(path, target):
        return move(path, target, real_replace)

    def rename(path, target):
        return move(path, target, real_rename)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(io, 'open', tracked_open)
        patch.setattr(builtins, 'open', tracked_open)
        patch.setattr(Path, 'read_text', read_text)
        patch.setattr(Path, 'write_text', write_text)
        patch.setattr(Path, 'replace', replace)
        patch.setattr(Path, 'rename', rename)
        patch.setattr(shutil, 'copyfile', copyfile)
        patch.setattr(shutil, 'copy2', copy2)
        try:
            yield
        finally:
            request.config._gpu_file_cache_stats = {**counts, 'entries': len(entries), 'characters': cached_chars}
            entries.clear()
            writers.clear()


@pytest.fixture(scope="session", autouse=True)
def gpu_pure_caches(request, gpu_text_files):
    """Cache syntax only; SQL evidence and every audit run remain fresh.

    Parsed state is private to each caller. Custom parsers and modified
    handlers deliberately bypass the cache, including the title regression
    harness. Exceptions are never cached. No cache survives this session.
    """
    from copy import deepcopy
    from functools import lru_cache
    from pipelines.gpu_prices import audit, i18n

    page_type = audit.Page
    original_init = page_type.__init__
    handler_names = ("reset", "feed", "close", "handle_starttag",
                     "handle_startendtag", "handle_data", "handle_endtag")
    handlers = tuple(getattr(page_type, name) for name in handler_names)
    cdata = page_type.CDATA_CONTENT_ELEMENTS

    @lru_cache(maxsize=768)
    def parsed_state(text):
        page = page_type.__new__(page_type)
        original_init(page, text)
        return page.__dict__

    def cached_init(self, text):
        if (type(self) is not page_type or not isinstance(text, str)
                or page_type.CDATA_CONTENT_ELEMENTS != cdata
                or tuple(getattr(page_type, name) for name in handler_names) != handlers):
            original_init(self, text)
        else:
            self.__dict__.update(deepcopy(parsed_state(text)))

    original_fields = i18n.placeholders

    @lru_cache(maxsize=8192)
    def field_names(template):
        return frozenset(original_fields(template))

    def cached_fields(template):
        if not isinstance(template, str):
            return original_fields(template)
        return set(field_names(template))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(page_type, "__init__", cached_init)
        patch.setattr(i18n, "placeholders", cached_fields)
        try:
            yield
        finally:
            request.config._gpu_cache_stats = {
                "html": parsed_state.cache_info()._asdict(),
                "placeholders": field_names.cache_info()._asdict(),
            }
            parsed_state.cache_clear()
            field_names.cache_clear()


def pytest_terminal_summary(terminalreporter, config):
    stats = getattr(config, "_gpu_cache_stats", None)
    if stats:
        terminalreporter.write_line("GPU session pure caches: " + str(stats))
    files = getattr(config, "_gpu_file_cache_stats", None)
    if files:
        terminalreporter.write_line("GPU session versioned text files: " + str(files))


@pytest.fixture
def gpu_site_copy(tmp_path):
    """Give corruption tests private files and SQL, never a writable shared site."""
    from copy import deepcopy
    import shutil
    from pipelines.gpu_prices.store import PriceStore

    stores = []
    serial = 0

    def duplicate(site):
        nonlocal serial
        folder = tmp_path / ("gpu-copy-" + str(serial))
        serial += 1
        folder.mkdir()
        out = folder / "site"
        shutil.copytree(site[1], out, copy_function=shutil.copy2)
        if isinstance(site[0], PriceStore):
            db = PriceStore(":memory:")
            site[0].connection.backup(db.connection)
            stores.append(db)
        else:
            db = folder / "prices.sqlite"
            with PriceStore(site[0], read_only=True) as source, PriceStore(db) as target:
                source.connection.backup(target.connection)
        return (db, out, *deepcopy(site[2:]))

    try:
        yield duplicate
    finally:
        for store in stores:
            store.close()
