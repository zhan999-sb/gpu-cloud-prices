"""CLI: python -m pipelines.gpu_prices.run collect|build|all|deploy [options]."""

import argparse
from http.client import HTTPException
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3
from time import perf_counter

from .build_site import DEFAULT_OUT, build_site
from .deploy import DeployError, deploy_site
from .providers.azure import AzureCollector
from .providers.linode import LinodeCollector
from .providers.scaleway import ScalewayCollector
from .providers.verda import VerdaCollector
from .fx import refresh, to_usd
from .schema import utc_now
from .store import DEFAULT_DB, PriceStore, ROOT
from .audit import audit_site, live_sample

LOG = logging.getLogger(__name__)


def positive_int(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def configure_logging() -> None:
    folder = Path(__file__).resolve().parent / "runtime"
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "gpu_prices.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
                        handlers=[handler, logging.StreamHandler()], force=True)
    import time
    logging.Formatter.converter = time.gmtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect verified public tier A GPU prices and build an offline static site.")
    parser.add_argument("command", choices=("collect", "build", "all", "deploy", "audit"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit-pages", type=positive_int, default=None, help="Limit successful pages; coverage may be partial")
    parser.add_argument("--provider", action="append", choices=("azure","linode","scaleway","verda"), help="Retry selected enabled providers; default collects all")
    parser.add_argument("--site-base-url", help="Public HTTPS origin; overrides SITE_BASE_URL (default: placeholder https://example.pages.dev)")
    parser.add_argument("--project", help="Cloudflare Pages project name (required for deploy)")
    parser.add_argument("--live-sample", type=positive_int, help="Audit N random stored Azure offers against the live API")
    args = parser.parse_args(argv)
    if args.live_sample and args.command != "audit":
        parser.error("--live-sample requires audit")
    if args.command == "deploy":
        if not args.project:
            parser.error("deploy requires --project")
        if args.out != DEFAULT_OUT or args.db != DEFAULT_DB or args.limit_pages is not None or args.site_base_url is not None:
            parser.error("deploy publishes the repository site/; collection/build options do not apply")
    configure_logging()
    if args.command == "deploy":
        try:
            result = deploy_site(args.project)
        except DeployError as exc:
            # DeployError contains only fixed, credential-free messages. Never log
            # subprocess output, dotenv contents, environment, or raw exceptions.
            LOG.error("%s", exc)
            print(json.dumps({"deploy": {"status": "failed", "error": str(exc)}}))
            return 1
        print(json.dumps({"deploy": result}, indent=2))
        return 0
    exit_code = 0
    summary = {}
    try:
        with PriceStore(args.db, read_only=args.command in {"build", "audit"} and args.db.exists()) as store:
            if args.command == "audit":
                summary["audit"] = audit_site(store.connection, args.out)
                if args.live_sample:
                    summary["live_sample"] = live_sample(store.connection, args.live_sample)
                    if summary["live_sample"]["status"] != "passed":
                        exit_code = 1
            if args.command in {"collect", "all"}:
                selected = set(args.provider or ("azure","linode","scaleway","verda"))
                rates, fx_status = refresh(args.db.parent / "ecb_rates.json") if 'scaleway' in selected else (None, 'not_required')
                summary["fx"] = {"status": fx_status, "date": rates["date"] if rates else None}
                collections = {}
                for name, factory in (("azure", AzureCollector), ("linode", LinodeCollector), ("scaleway", ScalewayCollector), ("verda", VerdaCollector)):
                    if name not in selected:
                        continue
                    collector = factory()
                    started = utc_now()
                    timer = perf_counter()
                    try:
                        offers = collector.collect(limit_pages=args.limit_pages)
                        offers = [to_usd(o, rates) for o in offers]
                        changed = store.upsert(offers)
                        status = "complete" if collector.complete else "limited"
                        store.record_run(provider=name, started_at=started, status=status,
                                         pages=collector.pages_fetched, records=len(offers))
                        collections[name] = {"status": status, "pages": collector.pages_fetched,
                            "raw_items": collector.raw_items, "records": len(offers), "rows_changed": changed,
                            "models": sorted({o.gpu_model for o in offers}),
                            "runtime_seconds": round(perf_counter() - timer, 3)}
                    except (OSError, ValueError, KeyError, TypeError, HTTPException) as exc:
                        LOG.exception("%s collection failed; no partial batch saved", name)
                        store.record_run(provider=name, started_at=started, status="failed",
                                         pages=collector.pages_fetched, records=0, detail=type(exc).__name__)
                        collections[name] = {"status": "failed", "pages": collector.pages_fetched,
                            "records": 0, "models": [], "error": str(exc),
                            "runtime_seconds": round(perf_counter() - timer, 3)}
                        exit_code = 1
                summary["collections"] = collections
                summary["collection"] = collections.get("azure", next(iter(collections.values())))
            if args.command in {"build", "all"}:
                summary["build"] = build_site(store, args.out, site_base_url=args.site_base_url)
                LOG.info("Static site built: %s", summary["build"])
    except (OSError, ValueError, HTTPException, sqlite3.Error) as exc:
        LOG.exception("Pipeline failed")
        summary["error"] = str(exc)
        exit_code = 1
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
