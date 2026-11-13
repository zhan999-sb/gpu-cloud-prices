"""Current daily listings and compressed daily group minima for history.

Daily history represents the cheapest offer in each provider/model/allocation/
type/region group at the final observation that day, not every marketplace host.
Legacy observations are retained losslessly in a migration archive and backup.
"""

from dataclasses import fields
from datetime import date, timedelta
from pathlib import Path
import sqlite3
import re
import json
import zlib
from statistics import median

from .schema import Offer, parse_utc, utc_now, validate_offer
from .regions import region_class
from .models import canonical_model

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = Path(__file__).parent / "runtime" / "gpu_prices.sqlite"
OFFER_COLUMNS = tuple(field.name for field in fields(Offer))


def pack_rows(rows):
    return zlib.compress(json.dumps(rows, ensure_ascii=False, separators=(',', ':'),
                                    sort_keys=True, allow_nan=False).encode('utf-8'), 9)


def unpack_rows(payload):
    return json.loads(zlib.decompress(payload))


def history_minima(rows):
    """One daily group point, with currency/scope kept separate for comparison."""
    groups = {}
    for row in rows:
        key = tuple(row[k] for k in ('provider', 'gpu_model', 'gpu_count', 'gpu_fraction',
                                    'price_type', 'region', 'currency', 'price_scope'))
        rank = (row['price_per_gpu_hour'] or row['price_per_hour'], row['sku'])
        if key not in groups or rank < (groups[key]['price_per_gpu_hour'] or groups[key]['price_per_hour'], groups[key]['sku']):
            groups[key] = row
    return sorted(groups.values(), key=lambda r: (r['gpu_model'], r['sku'], r['region'] or '', r['price_type']))


class PriceStore:
    def __init__(self, path: str | Path = DEFAULT_DB, *, read_only=False):
        self._rows_cache = None
        self.backup_path = None
        if read_only:
            self.connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
            self.connection.row_factory = sqlite3.Row
            return
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        if Path(path) == DEFAULT_DB and not Path(path).exists() and (ROOT / 'data' / 'gpu_prices.sqlite').exists():
            # Work in the pipeline-owned database; the operator's original
            # history is read-only and copied through SQLite's backup API.
            with sqlite3.connect((ROOT / 'data' / 'gpu_prices.sqlite').resolve().as_uri()+'?mode=ro',uri=True) as source, sqlite3.connect(path) as destination:
                source.backup(destination)
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        tables = {r[0] for r in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        migrated = 'meta' in tables and self.connection.execute("SELECT 1 FROM meta WHERE key='compact_history_v1'").fetchone()
        # Back up before ANY schema/data migration. SQLite backup includes WAL.
        if 'offers' in tables and not migrated and str(path) != ':memory:':
            stamp = utc_now().replace(':', '').replace('-', '').replace('.', '')
            self.backup_path = Path(path).with_name(Path(path).stem + '.backup-' + stamp + '.sqlite')
            with sqlite3.connect(self.backup_path) as backup:
                self.connection.backup(backup)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS offers (
                provider TEXT NOT NULL, sku TEXT NOT NULL, gpu_model TEXT NOT NULL,
                gpu_count REAL NOT NULL CHECK(gpu_count > 0), vcpu INTEGER NOT NULL,
                ram_gb REAL NOT NULL, region TEXT NOT NULL, price_type TEXT NOT NULL,
                currency TEXT NOT NULL, price_per_hour REAL NOT NULL CHECK(price_per_hour > 0),
                price_per_gpu_hour REAL NOT NULL CHECK(price_per_gpu_hour > 0),
                source_url TEXT NOT NULL, collected_at TEXT NOT NULL, day TEXT NOT NULL,
                PRIMARY KEY (provider, sku, region, price_type, day)
            );
            CREATE INDEX IF NOT EXISTS offers_model_day ON offers(gpu_model, day);
            CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY, provider TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL, status TEXT NOT NULL,
                pages INTEGER NOT NULL, records INTEGER NOT NULL, detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS guide_reference_models (
                model TEXT PRIMARY KEY, parameters_billions REAL NOT NULL,
                source_url TEXT NOT NULL
            );
        """)
        from .guide_specs import REFERENCE_MODELS
        self.connection.executemany('INSERT OR IGNORE INTO guide_reference_models VALUES (?,?,?)', REFERENCE_MODELS)
        if "effective_start_date" not in {r[1] for r in self.connection.execute("PRAGMA table_info(offers)")}:
            self.connection.execute("ALTER TABLE offers ADD COLUMN effective_start_date TEXT")
        # Additive migration: retain all original keys, prices and Azure history.
        from dataclasses import MISSING
        existing = {r[1] for r in self.connection.execute("PRAGMA table_info(offers)")}
        for field in fields(Offer):
            if field.name in existing:
                continue
            default = field.default
            if default is MISSING:
                raise ValueError("missing required migration column: " + field.name)
            kind = "INTEGER" if isinstance(default, bool) or field.name == "minimum_billing_seconds" else "REAL" if isinstance(default, float) or field.name in {"original_price", "fx_rate", "price_per_day", "fx_usd_per_eur", "fx_original_per_eur"} else "TEXT"
            literal = "NULL" if default is None else str(int(default)) if isinstance(default, bool) else str(default) if isinstance(default, float) else "'" + default.replace("'", "''") + "'"
            self.connection.execute(f"ALTER TABLE offers ADD COLUMN {field.name} {kind} DEFAULT {literal}")
        self.connection.commit()
        # SQLite cannot drop NOT NULL in place. Transactionally rebuild only
        # when opening an older table; every column and row is copied verbatim.
        info = self.connection.execute("PRAGMA table_info(offers)").fetchall()
        nullable = {"vcpu", "ram_gb", "gpu_count", "gpu_fraction", "price_per_gpu_hour"}
        if any(r[1] in nullable and r[3] for r in info):
            with self.connection:
                sql = self.connection.execute("SELECT sql FROM sqlite_master WHERE name='offers'").fetchone()[0]
                # A previous SQLite RENAME quotes the table name. Support both
                # original and already-migrated databases without changing data.
                sql = re.sub(r'^CREATE TABLE\s+(?:"offers"|offers)\s*\(',
                             'CREATE TABLE offers_migrated (', sql, count=1, flags=re.I)
                for column in nullable:
                    kind = "INTEGER" if column == "vcpu" else "REAL"
                    sql = sql.replace(f"{column} {kind} NOT NULL", f"{column} {kind}")
                self.connection.execute(sql)
                names = ",".join(r[1] for r in info)
                self.connection.execute(f"INSERT INTO offers_migrated ({names}) SELECT {names} FROM offers")
                self.connection.execute("DROP TABLE offers")
                self.connection.execute("ALTER TABLE offers_migrated RENAME TO offers")
                self.connection.execute("CREATE INDEX offers_model_day ON offers(gpu_model, day)")
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS history_snapshots (
                provider TEXT NOT NULL, day TEXT NOT NULL, row_count INTEGER NOT NULL,
                payload BLOB NOT NULL, PRIMARY KEY(provider, day)
            );
            CREATE TABLE IF NOT EXISTS legacy_archive (
                name TEXT PRIMARY KEY, row_count INTEGER NOT NULL, payload BLOB NOT NULL
            );
        ''')
        if not migrated:
            with self.connection:
                for table in ('offers', 'collection_runs'):
                    original = [dict(r) for r in self.connection.execute('SELECT * FROM ' + table)]
                    if original:
                        self.connection.execute('INSERT OR IGNORE INTO legacy_archive VALUES (?,?,?)',
                                                (table, len(original), pack_rows(original)))
                self._archive_old_days()
                self._prune_runs()
                self.connection.execute("INSERT INTO meta VALUES ('compact_history_v1','1')")
            if self.backup_path:
                self.connection.execute('VACUUM')

    def _archive_old_days(self):
        old = self.connection.execute('''SELECT provider,day FROM offers GROUP BY provider,day
            HAVING day < (SELECT MAX(b.day) FROM offers b WHERE b.provider=offers.provider)''').fetchall()
        for provider, day in old:
            rows = [dict(r) for r in self.connection.execute('SELECT * FROM offers WHERE provider=? AND day=?', (provider, day))]
            previous = self.connection.execute('SELECT payload FROM history_snapshots WHERE provider=? AND day=?', (provider, day)).fetchone()
            if previous:
                by_key = {(r['sku'], r['region'], r['price_type']): r for r in unpack_rows(previous[0])}
                for row in rows:
                    key = row['sku'], row['region'], row['price_type']
                    if key not in by_key or row['collected_at'] > by_key[key]['collected_at']:
                        by_key[key] = row
                rows = list(by_key.values())
            rows = history_minima(rows)
            self.connection.execute('INSERT INTO history_snapshots VALUES (?,?,?,?) ON CONFLICT(provider,day) DO UPDATE SET row_count=excluded.row_count,payload=excluded.payload',
                                    (provider, day, len(rows), pack_rows(rows)))
            self.connection.execute('DELETE FROM offers WHERE provider=? AND day=?', (provider, day))

    def _prune_runs(self):
        # Keep the latest outcome and latest success for honest freshness/health.
        self.connection.execute('''DELETE FROM collection_runs WHERE id NOT IN
            (SELECT MAX(id) FROM collection_runs GROUP BY provider) AND id NOT IN
            (SELECT MAX(id) FROM collection_runs WHERE status='complete' GROUP BY provider)''')

    def observation_rows(self):
        if self._rows_cache is None:
            rows = [dict(r) for r in self.connection.execute('SELECT * FROM offers')]
            tables = {r[0] for r in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'history_snapshots' in tables:
                for snapshot in self.connection.execute('SELECT payload FROM history_snapshots ORDER BY day,provider'):
                    rows.extend(unpack_rows(snapshot[0]))
            # A reversible read view also fixes old observations/history. The
            # SQLite source, raw name and raw capacity remain untouched.
            for row in rows:
                row['gpu_model'] = canonical_model(row['gpu_model'], row.get('vram_raw'))
            self._rows_cache = rows
        return self._rows_cache

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self) -> None:
        self.connection.close()

    def claim_page_fetch(self, provider: str, stamp: str, *, interval_hours: int = 1) -> bool:
        """Reserve the hourly request before IO, including failed attempts.

        BEGIN IMMEDIATE serializes overlapping workflow processes. Skipping does
        not replace the last collection run or pretend that prices were fetched.
        """
        if type(interval_hours) is not int or interval_hours < 1:
            raise ValueError('interval_hours must be a positive integer')
        now = parse_utc(stamp)
        key = "page_last_fetch:" + provider
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row and now - parse_utc(row[0]) < timedelta(hours=interval_hours):
                self.connection.rollback()
                return False
            self.connection.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, stamp))
            # The data branch normally saves only changed prices or a new day.
            # A new request budget MUST also survive a fresh Actions checkout,
            # even if prices stayed identical (or the request subsequently fails).
            # Invalidate the save receipt, without changing the price fingerprint
            # or the successful deployment receipt/build decision.
            self.connection.execute("DELETE FROM meta WHERE key='saved_fingerprint'")
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def upsert(self, offers) -> int:
        """Atomic batch; newer observation wins. An identical replay changes no rows."""
        batch = []
        for offer in offers:
            validate_offer(offer)
            # Canonical UTC precision makes lexical comparisons chronological.
            stamp = parse_utc(offer.collected_at).isoformat(timespec="microseconds").replace("+00:00", "Z")
            values = offer.to_dict()
            values["collected_at"] = stamp
            # Empty SQL sentinel makes unknown-region keys idempotent; it is
            # always restored to null at the public schema boundary.
            values["region"] = offer.region or ""
            batch.append(tuple(values[name] for name in OFFER_COLUMNS) + (offer.day,))
        columns = ", ".join(OFFER_COLUMNS + ("day",))
        marks = ", ".join("?" for _ in range(len(OFFER_COLUMNS) + 1))
        updates = ", ".join(f"{name}=excluded.{name}" for name in OFFER_COLUMNS)
        changed = 0
        with self.connection:
            # Marketplace hosts are transient. Replace its current listing only
            # for a newer complete batch; never accumulate disappeared SKUs.
            clore = [r for r in batch if r[0] == 'clore']
            if clore:
                newest = max(r[OFFER_COLUMNS.index('collected_at')] for r in clore)
                latest = self.connection.execute("SELECT MAX(collected_at) FROM offers WHERE provider='clore'").fetchone()[0]
                if latest and newest > latest:
                    # Archive the preceding day before removing its host rows.
                    day = newest[:10]
                    if latest[:10] < day:
                        rows = history_minima([dict(r) for r in self.connection.execute("SELECT * FROM offers WHERE provider='clore'")])
                        self.connection.execute('INSERT OR REPLACE INTO history_snapshots VALUES (?,?,?,?)', ('clore', latest[:10], len(rows), pack_rows(rows)))
                    self.connection.execute("DELETE FROM offers WHERE provider='clore'")
                elif latest and newest < latest:
                    batch = [r for r in batch if r[0] != 'clore']
            before = self.connection.total_changes
            self.connection.executemany(f"""
                INSERT INTO offers ({columns}) VALUES ({marks})
                ON CONFLICT(provider, sku, region, price_type, day) DO UPDATE SET {updates}
                WHERE excluded.collected_at > offers.collected_at
            """, batch)
            changed = self.connection.total_changes - before
            self._archive_old_days()
        self._rows_cache = None
        return changed

    def latest_prices(self, *, day: str | None = None, gpu_model: str | None = None) -> list[Offer]:
        """Latest observation per key, or exactly a UTC day (never fill today's gaps)."""
        if day is not None:
            date.fromisoformat(day)
        latest = {}
        for row in self.observation_rows():
            if (day is not None and row['day'] != day) or (gpu_model is not None and row['gpu_model'] != gpu_model):
                continue
            key = row['provider'], row['sku'], row['region'], row['price_type']
            if key not in latest or row['collected_at'] > latest[key]['collected_at']:
                latest[key] = row
        rows = sorted(latest.values(), key=lambda r: (r['currency'], r['price_per_gpu_hour'] is not None,
                      r['price_per_gpu_hour'] or 0, r['provider'], r['region'] or '', r['sku'], r['price_type']))
        return [Offer(**{name: bool(row[name]) if name in {'is_from_price', 'export_allowed'} else
                       (row[name] or None) if name == 'region' else row[name] for name in OFFER_COLUMNS}) for row in rows]

    def models(self) -> list[str]:
        return sorted({row['gpu_model'] for row in self.observation_rows()})

    def daily_series(self, gpu_model: str, *, currency: str = "USD", price_type: str | None = None, export_only=False) -> list[dict]:
        """Unweighted cross-offer daily distribution; never aggregate currencies."""
        grouped = {}
        for row in sorted(self.observation_rows(), key=lambda r: r['day']):
            if (row['gpu_model'] != gpu_model or row['currency'] != currency or row['gpu_count'] is None or row['gpu_count'] < 1
                    or price_type and row['price_type'] != price_type or export_only and not (row['tier']=='A' and row['export_allowed'])):
                continue
            offer = Offer(**{name: bool(row[name]) if name in {"is_from_price", "export_allowed"} else (row[name] or None) if name == "region" else row[name] for name in OFFER_COLUMNS})
            from .regions import eligible
            if eligible(offer, currency=currency) and offer.current_at(row["day"] + "T23:59:59.999999Z"):
                grouped.setdefault(row["day"], []).append(offer.price_per_gpu_hour)
        return [{"day": day, "currency": currency, "price_type": price_type or "all",
                 "min": min(values), "median": median(values), "max": max(values), "count": len(values)}
                for day, values in grouped.items()]

    def last_updated(self, gpu_model: str | None = None) -> str | None:
        return max((r['collected_at'] for r in self.observation_rows() if gpu_model is None or r['gpu_model']==gpu_model), default=None)

    def record_run(self, *, provider: str, started_at: str, status: str, pages: int, records: int, detail: str = "") -> None:
        if status not in {"complete", "limited", "failed"}:
            raise ValueError("invalid collection status")
        parse_utc(started_at)
        with self.connection:
            self.connection.execute("""INSERT INTO collection_runs
                (provider, started_at, finished_at, status, pages, records, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (provider, started_at, utc_now(), status, pages, records, detail))
            self._prune_runs()

    def latest_run(self) -> dict | None:
        row = self.connection.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def provider_runs(self) -> dict:
        rows = self.connection.execute("SELECT * FROM collection_runs ORDER BY id")
        return {row["provider"]: dict(row) for row in rows}

    def current_prices(self, day, stamp):
        runs = self.provider_runs()
        result = []
        for offer in self.latest_prices(day=day):
            run = runs.get(offer.provider)
            if run and (run["status"] == "failed" or run["started_at"][:10] != day):
                continue
            if run and run["status"] == "complete" and not (parse_utc(run["started_at"]) <= parse_utc(offer.collected_at) <= parse_utc(run["finished_at"])):
                continue
            if offer.current_at(stamp):
                result.append(offer)
        return result
