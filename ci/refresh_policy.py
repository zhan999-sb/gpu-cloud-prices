"""Offline refresh decisions and SQLite receipts; never invokes a service API.

The data branch stores price changes and the first snapshot of each UTC day.
Actions cache separately persists a tiny SQLite deployment receipt, so a
freshness-only deployment does not require a data-branch commit. A missing or
evicted receipt can only cause an extra build, never suppress a needed deploy.
"""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from pipelines.gpu_prices.schema import parse_utc
from pipelines.gpu_prices.store import PriceStore


MAX_DEPLOY_AGE = timedelta(hours=6)
DEPLOY_KEYS = ("deployed_fingerprint", "last_deploy_at")
OBSERVATION_FIELDS = {"collected_at", "availability_observed_at"}
NUMERIC_FIELDS = {"gpu_count", "gpu_fraction", "ram_gb", "price_per_hour",
                  "price_per_gpu_hour", "original_price", "fx_rate"}


def _now(now):
    value = now if now is not None else datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("refresh clock must have an explicit timezone")
    return value.astimezone(timezone.utc)


def _meta(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return dict(conn.execute("SELECT key, value FROM meta"))


def _put(conn, values):
    with conn:
        conn.executemany("INSERT INTO meta(key,value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())


def fingerprint(store, *, now=None):
    """Hash the current provider population, never historical or observed times.

    Include all substantive offer fields (including effective intervals, price
    scope, tier, original currency/price and FX inputs). Canonical JSON and row
    ordering ignore insertion order, SQL order, locale and platform separators.
    """
    stamp = _now(now)
    rows = []
    for offer in store.current_prices(stamp.date().isoformat(), stamp.isoformat()):
        row = {k: v for k, v in offer.to_dict().items() if k not in OBSERVATION_FIELDS}
        for key in NUMERIC_FIELDS:
            if row[key] is not None:
                row[key] = float(row[key])
        for key in ("effective_start_date", "effective_from", "effective_to"):
            if row[key]:
                row[key] = parse_utc(row[key]).isoformat(timespec="microseconds")
        rows.append(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))
    payload = json.dumps(sorted(rows), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _deployment(meta, now):
    try:
        stamp = parse_utc(meta["last_deploy_at"])
        digest = meta["deployed_fingerprint"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or stamp > now:
            return None
        return stamp, digest
    except (KeyError, TypeError, ValueError):
        return None


def restore_receipt(store, receipt, *, now=None):
    """Merge only a valid, newer successful deploy; never price/save metadata."""
    now = _now(now)
    path = Path(receipt)
    if not path.is_file():
        return
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            cached = dict(conn.execute("SELECT key,value FROM meta"))
    except sqlite3.Error:
        return  # A bad cache is a miss. The price DB itself still fails closed.
    new = _deployment(cached, now)
    current = _deployment(_meta(store.connection), now)
    if new and (not current or new[0] > current[0]):
        _put(store.connection, {key: cached[key] for key in DEPLOY_KEYS})


def decide(store, *, now=None):
    now = _now(now)
    digest = fingerprint(store, now=now)
    meta = _meta(store.connection)
    deployed = _deployment(meta, now)
    changed = digest != meta.get("saved_fingerprint")
    new_day = now.date().isoformat() != meta.get("last_saved_utc_day")
    build = not deployed or digest != deployed[1] or now - deployed[0] >= MAX_DEPLOY_AGE
    return {"fingerprint": digest, "snapshot_at": now.isoformat(), "changed": changed, "new_utc_day": new_day,
            "save": changed or new_day, "build": build,
            "reason": "price_snapshot_or_deploy_age" if build else "unchanged_and_recently_deployed"}


def record_deployed(store, receipt, digest, *, now=None, snapshot_at=None):
    """Call only after the deploy command succeeded, with its build digest."""
    now = _now(now)
    # A deployment can finish after UTC midnight or an offer's expiry. Verify
    # the actual build snapshot at its decision clock, then stamp success now.
    snapshot_clock = parse_utc(snapshot_at) if snapshot_at else now
    if snapshot_clock > now or digest != fingerprint(store, now=snapshot_clock):
        raise ValueError("price snapshot changed after the build decision; refusing deployment receipt")
    values = {"deployed_fingerprint": digest, "last_deploy_at": now.isoformat()}
    _meta(store.connection)
    _put(store.connection, values)
    path = Path(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.deploy-', suffix='.sqlite', dir=path.parent)
    os.close(handle)
    try:
        with closing(sqlite3.connect(temporary)) as conn:
            _meta(conn)
            _put(conn, values)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare_save(store, *, now=None):
    """Stamp the snapshot being saved. A rejected push retains it for recovery.

    The next fresh Actions checkout restores the old data-branch stamp and
    therefore retries the save. Do not mark a failed deploy as successful here.
    """
    now = _now(now)
    decision = decide(store, now=now)
    if decision["save"]:
        _put(store.connection, {"saved_fingerprint": decision["fingerprint"],
                                "last_saved_utc_day": now.date().isoformat()})
    return decision["save"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("decide", "deployed", "prepare-save"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--fingerprint")
    parser.add_argument("--snapshot-at")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "deployed" and (not args.receipt or not args.fingerprint):
        parser.error("deployed requires --receipt and --fingerprint")
    with PriceStore(args.db) as store:
        if args.command == "decide":
            if args.receipt:
                restore_receipt(store, args.receipt)
            result = decide(store)
            if args.github_output:
                with args.github_output.open("a", encoding="utf-8", newline="\n") as output:
                    for key in ("build", "save", "fingerprint", "snapshot_at"):
                        output.write(key + "=" + json.dumps(result[key]).strip('"') + "\n")
            print(json.dumps(result, sort_keys=True))
        elif args.command == "deployed":
            record_deployed(store, args.receipt, args.fingerprint, snapshot_at=args.snapshot_at)
        else:
            print(str(prepare_save(store)).lower())


if __name__ == "__main__":
    main()
