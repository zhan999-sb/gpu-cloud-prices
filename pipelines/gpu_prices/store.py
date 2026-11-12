"""One last observation per offer/day, plus collection coverage provenance."""

from dataclasses import fields
from datetime import date
from pathlib import Path
import sqlite3
from statistics import median

from .schema import Offer, parse_utc, utc_now, validate_offer
from .regions import region_class

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = Path(__file__).parent / "runtime" / "gpu_prices.sqlite"
OFFER_COLUMNS = tuple(field.name for field in fields(Offer))


class PriceStore:
    def __init__(self, path: str | Path = DEFAULT_DB, *, read_only=False):
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
        """)
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
            kind = "INTEGER" if isinstance(default, bool) or field.name == "minimum_billing_seconds" else "REAL" if isinstance(default, float) or field.name in {"original_price", "fx_rate"} else "TEXT"
            literal = "NULL" if default is None else str(int(default)) if isinstance(default, bool) else str(default) if isinstance(default, float) else "'" + default.replace("'", "''") + "'"
            self.connection.execute(f"ALTER TABLE offers ADD COLUMN {field.name} {kind} DEFAULT {literal}")
        self.connection.commit()
        # SQLite cannot drop NOT NULL in place. Transactionally rebuild only
        # when opening an older table; every column and row is copied verbatim.
        info = self.connection.execute("PRAGMA table_info(offers)").fetchall()
        if any(r[1] in {"vcpu", "ram_gb"} and r[3] for r in info):
            with self.connection:
                sql = self.connection.execute("SELECT sql FROM sqlite_master WHERE name='offers'").fetchone()[0]
                sql = sql.replace("offers (", "offers_migrated (").replace("vcpu INTEGER NOT NULL", "vcpu INTEGER").replace("ram_gb REAL NOT NULL", "ram_gb REAL")
                self.connection.execute(sql)
                names = ",".join(r[1] for r in info)
                self.connection.execute(f"INSERT INTO offers_migrated ({names}) SELECT {names} FROM offers")
                self.connection.execute("DROP TABLE offers")
                self.connection.execute("ALTER TABLE offers_migrated RENAME TO offers")
                self.connection.execute("CREATE INDEX offers_model_day ON offers(gpu_model, day)")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self) -> None:
        self.connection.close()

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
        before = self.connection.total_changes
        with self.connection:
            self.connection.executemany(f"""
                INSERT INTO offers ({columns}) VALUES ({marks})
                ON CONFLICT(provider, sku, region, price_type, day) DO UPDATE SET {updates}
                WHERE excluded.collected_at > offers.collected_at
            """, batch)
        return self.connection.total_changes - before

    def latest_prices(self, *, day: str | None = None, gpu_model: str | None = None) -> list[Offer]:
        """Latest observation per key, or exactly a UTC day (never fill today's gaps)."""
        conditions, parameters = [], []
        if day is not None:
            date.fromisoformat(day)
            conditions.append("day = ?")
            parameters.append(day)
        if gpu_model is not None:
            conditions.append("gpu_model = ?")
            parameters.append(gpu_model)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self.connection.execute(f"""
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY provider, sku, region, price_type ORDER BY collected_at DESC
                ) AS row_number FROM offers {where}
            ) WHERE row_number = 1 ORDER BY currency, price_per_gpu_hour, provider, region, sku, price_type
        """, parameters).fetchall()
        return [Offer(**{name: bool(row[name]) if name in {"is_from_price", "export_allowed"} else (row[name] or None) if name == "region" else row[name]
                         for name in OFFER_COLUMNS if name in row.keys()}) for row in rows]

    def models(self) -> list[str]:
        return [row[0] for row in self.connection.execute("SELECT DISTINCT gpu_model FROM offers ORDER BY gpu_model")]

    def daily_series(self, gpu_model: str, *, currency: str = "USD", price_type: str | None = None, export_only=False) -> list[dict]:
        """Unweighted cross-offer daily distribution; never aggregate currencies."""
        query = "SELECT * FROM offers WHERE gpu_model=? AND currency=? AND gpu_count>=1"
        parameters = [gpu_model, currency]
        if price_type:
            query += " AND price_type=?"
            parameters.append(price_type)
        if export_only:
            query += " AND tier='A' AND export_allowed=1"
        grouped = {}
        for row in self.connection.execute(query + " ORDER BY day", parameters):
            offer = Offer(**{name: bool(row[name]) if name in {"is_from_price", "export_allowed"} else (row[name] or None) if name == "region" else row[name] for name in OFFER_COLUMNS})
            from .regions import eligible
            if eligible(offer, currency=currency) and offer.current_at(row["day"] + "T23:59:59.999999Z"):
                grouped.setdefault(row["day"], []).append(offer.price_per_gpu_hour)
        return [{"day": day, "currency": currency, "price_type": price_type or "all",
                 "min": min(values), "median": median(values), "max": max(values), "count": len(values)}
                for day, values in grouped.items()]

    def last_updated(self, gpu_model: str | None = None) -> str | None:
        if gpu_model is None:
            return self.connection.execute("SELECT MAX(collected_at) FROM offers").fetchone()[0]
        return self.connection.execute("SELECT MAX(collected_at) FROM offers WHERE gpu_model=?", (gpu_model,)).fetchone()[0]

    def record_run(self, *, provider: str, started_at: str, status: str, pages: int, records: int, detail: str = "") -> None:
        if status not in {"complete", "limited", "failed"}:
            raise ValueError("invalid collection status")
        parse_utc(started_at)
        with self.connection:
            self.connection.execute("""INSERT INTO collection_runs
                (provider, started_at, finished_at, status, pages, records, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (provider, started_at, utc_now(), status, pages, records, detail))

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
