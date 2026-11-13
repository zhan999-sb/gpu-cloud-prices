"""Notify IndexNow only after a successful deploy; persist state in its receipt.

The production workflow serializes receipt writers. Reserve attempted URLs before
HTTP, including failures, to enforce our 10,000/UTC-day budget across CI runs.
Unsent/failed changes survive later deployments; unchanged pages are never added
to the queue. A missing cache establishes a new baseline (all indexable pages).
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pipelines.gpu_prices.indexnow import KEY

ENDPOINT = 'https://api.indexnow.org/indexnow'
DAILY_LIMIT = 10_000
DELETED = 'deleted'


def load_manifest(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    origin = data['origin']
    parts = urlsplit(origin)
    if (data['version'] != 1 or parts.scheme != 'https' or not parts.hostname
            or parts.netloc != parts.hostname or parts.path or parts.query or parts.fragment):
        raise ValueError('Invalid IndexNow origin/version')
    if not isinstance(data['pages'], dict):
        raise ValueError('Invalid IndexNow page hashes')
    for url, digest in data['pages'].items():
        target = urlsplit(url)
        if (not url.startswith(origin+'/') or target.netloc != parts.netloc or target.scheme != 'https'
                or target.query or target.fragment or any(c.isspace() or c in '<>"\\' for c in url)
                or any(s in {'.','..'} for s in target.path.split('/'))
                or not re.fullmatch(r'[a-f0-9]{64}', digest)):
            raise ValueError('Invalid IndexNow page URL/hash')
    return data


def post(payload):
    request = Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=True).encode('utf-8'),
                      headers={'Content-Type':'application/json; charset=utf-8'}, method='POST')
    with urlopen(request, timeout=20) as response:
        return response.status


def notify(manifest, receipt, *, now=None, sender=post):
    """Persist a deployed baseline and send a single bounded batch, if needed."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('IndexNow clock must include a timezone')
    day = now.astimezone(timezone.utc).date().isoformat()
    data = load_manifest(manifest)
    origin, current = data['origin'], data['pages']
    keyfile = Path(manifest).parent/(KEY+'.txt')
    if keyfile.read_bytes() != (KEY+'\n').encode('utf-8'):
        raise ValueError('IndexNow public ownership key file does not match')
    path = Path(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        known_baseline = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='indexnow_pages'").fetchone() is not None
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS indexnow_pages(url TEXT PRIMARY KEY, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS indexnow_pending(url TEXT PRIMARY KEY, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS indexnow_daily(day TEXT PRIMARY KEY, attempted INTEGER NOT NULL);
        ''')
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = dict(conn.execute('SELECT url,digest FROM indexnow_pages'))
            # Never send old-host URLs when a project changes its public origin.
            previous = {u:h for u,h in previous.items() if u.startswith(origin+'/')}
            for url, in conn.execute('SELECT url FROM indexnow_pending').fetchall():
                if not url.startswith(origin+'/'):
                    conn.execute('DELETE FROM indexnow_pending WHERE url=?',(url,))
            changed = {u:h for u,h in current.items() if previous.get(u) != h} if known_baseline else {}
            changed.update((u,DELETED) for u in previous.keys()-current.keys())
            conn.executemany('INSERT INTO indexnow_pending VALUES (?,?) ON CONFLICT(url) DO UPDATE SET digest=excluded.digest', changed.items())
            conn.execute('DELETE FROM indexnow_pages')
            conn.executemany('INSERT INTO indexnow_pages VALUES (?,?)', current.items())
            if not known_baseline:
                # Unknown previous deploy/UTC budget (first install or cache
                # eviction): seed without a ping and close today's budget.
                # Never turn a cache miss into a whole-site resubmission.
                conn.execute('INSERT INTO indexnow_daily VALUES (?,?) ON CONFLICT(day) DO UPDATE SET attempted=MAX(attempted,excluded.attempted)',(day,DAILY_LIMIT))
            used = conn.execute('SELECT attempted FROM indexnow_daily WHERE day=?',(day,)).fetchone()
            remaining = max(0, DAILY_LIMIT-(used[0] if used else 0))
            batch = conn.execute('SELECT url,digest FROM indexnow_pending ORDER BY url LIMIT ?',(remaining,)).fetchall()
            pending = conn.execute('SELECT COUNT(*) FROM indexnow_pending').fetchone()[0]
            if batch:
                conn.execute('INSERT INTO indexnow_daily VALUES (?,?) ON CONFLICT(day) DO UPDATE SET attempted=attempted+excluded.attempted',(day,len(batch)))
        result = {'changed':len(changed), 'attempted':len(batch), 'pending':pending, 'utc_day':day}
        if not known_baseline:
            return {**result, 'status':'baseline_initialized'}
        if not batch:
            return {**result, 'status':'daily_limit' if pending else 'unchanged'}
        payload = {'host':urlsplit(origin).hostname, 'key':KEY, 'keyLocation':origin+'/'+KEY+'.txt',
                   'urlList':[url for url,_ in batch]}
        try:
            status = sender(payload)
            if status not in (200,202):
                return {**result, 'status':'failed', 'http_status':status}
        except (OSError, ValueError) as exc:
            return {**result, 'status':'failed', 'error':type(exc).__name__}
        with conn:
            conn.executemany('DELETE FROM indexnow_pending WHERE url=? AND digest=?',batch)
        return {**result, 'status':'submitted', 'http_status':status, 'pending':pending-len(batch)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--receipt',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=notify(args.manifest,args.receipt)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        result={'status':'skipped_error','error':type(exc).__name__}
    print(json.dumps(result,sort_keys=True))
    return 0  # Notification failure must never undo a successful deployment.


if __name__=='__main__':
    raise SystemExit(main())
