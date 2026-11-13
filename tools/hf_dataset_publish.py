"""Publish a daily snapshot of the GPU Price Index open (tier-A) data to a Hugging Face dataset.

Source: the public, already-published exports at https://gpu-cloud-prices.pages.dev/data/latest.json
(tier-A, export-allowed only). Nothing private or display-only is read.

Usage: python tools/hf_dataset_publish.py [--repo ComputeTicks/gpu-cloud-prices] [--dry-run]
Token: HF_TOKEN from the environment, or from the local locked .env (never printed).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = "https://gpu-cloud-prices.pages.dev"
INDEX = f"{SITE}/data/latest.json"
OFFER_FIELDS = ["observation_day", "gpu_model", "provider", "sku", "gpu_count", "gpu_fraction", "vcpu", "ram_gb", "region",
                "price_type", "currency", "price_per_hour", "price_per_gpu_hour", "original_currency", "original_price",
                "price_scope", "billing_unit", "tax_basis", "availability", "tier", "collected_at", "effective_from",
                "effective_to", "source_url"]
STAT_FIELDS = ["gpu_model", "price_type", "day", "currency", "min", "median", "max", "count"]


def fetch_json(url: str):
    last = None
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gpu-price-index-dataset/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except Exception as exc:  # noqa: BLE001 - flaky network: retry
            last = exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"fetch failed after retries: {url} ({type(last).__name__})")


def token() -> str | None:
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    if os.environ.get("CI"):
        return None  # CI without the secret: skip publishing, never fail the workflow
    sys.path.insert(0, str(ROOT))
    from orchestrator.dotenv import parse_file  # local helper; reads the locked .env in-process
    value = parse_file(ROOT / ".env").get("HF_TOKEN")
    if not value:
        raise SystemExit("HF_TOKEN missing")
    return value


def collect():
    index = fetch_json(INDEX)
    offers, stats = [], []
    for item in index["exports"]:
        data = fetch_json(item["url"])
        day = data.get("observation_day") or index.get("observation_day")
        for offer in data.get("offers", []):
            if offer.get("tier") != "A" or offer.get("export_allowed") is False:
                continue
            row = {k: offer.get(k) for k in OFFER_FIELDS}
            row["observation_day"] = day
            offers.append(row)
        for price_type, points in (data.get("history") or {}).items():
            for p in points:
                stats.append({"gpu_model": data["gpu_model"], "price_type": p.get("price_type", price_type),
                              "day": p["day"], "currency": p.get("currency"), "min": p.get("min"),
                              "median": p.get("median"), "max": p.get("max"), "count": p.get("count")})
    return index, offers, stats


def write_csv(path: Path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_parquet(path: Path, rows, fields):
    import pyarrow as pa
    import pyarrow.parquet as pq
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist([{k: r.get(k) for k in fields} for r in rows])
    pq.write_table(table, path, compression="zstd")


def card(index, n_offers, n_models, n_providers, repo):
    day = index.get("observation_day")
    return f"""---
license: cc-by-4.0
pretty_name: GPU Cloud Prices (GPU Price Index)
language:
- en
tags:
- gpu
- cloud
- pricing
- compute
- llm-infrastructure
size_categories:
- 1K<n<10K
configs:
- config_name: offers_latest
  data_files: offers_latest.parquet
  default: true
- config_name: daily_stats
  data_files: daily_stats.parquet
- config_name: offers_by_day
  data_files: offers/*.parquet
---

# GPU Cloud Prices — daily public price snapshots

Daily snapshots of **public GPU cloud list prices** (on-demand and spot) compiled by the free, independent
[GPU Price Index]({SITE}/) ([open data page]({SITE}/data/)). Each row is one offer with provider, SKU, GPU model
and count, region, price type, hourly instance price and **per-GPU-hour** price, original currency, tax basis,
collection time and the official source URL.

- Latest snapshot: **{day}** — {n_offers:,} offers, {n_models} GPU models, {n_providers} providers.
- Only data from official public price APIs that we are allowed to re-export (tier A) is included.
- `offers_latest` — the newest snapshot; `offers_by_day` — one file per day; `daily_stats` — per model/price-type daily min/median/max.

## Caveats

- These are **list prices**, not guaranteed availability or final bills (taxes, storage, egress and minimum billing may apply).
- Spot/interruptible prices can change at any time. Always check the provider's page before renting.
- Fractional GPUs keep `gpu_fraction` < 1; per-GPU-hour prices are normalised accordingly.

## License and attribution

Our compilation is licensed **CC BY 4.0** — please attribute "GPU Price Index ({SITE}/)". The licence covers
our compilation only; provider names, trademarks and any provider rights are reserved by their owners.
Source code: https://github.com/zhan999-sb/gpu-cloud-prices. Dataset repo: `{repo}`.
"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default="ComputeTicks/gpu-cloud-prices")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    index, offers, stats = collect()
    day = index["observation_day"]
    models = {o["gpu_model"] for o in offers}
    providers = {o["provider"] for o in offers}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        write_parquet(out / "offers_latest.parquet", offers, OFFER_FIELDS)
        write_csv(out / "offers_latest.csv", offers, OFFER_FIELDS)
        write_parquet(out / "offers" / f"{day}.parquet", offers, OFFER_FIELDS)
        write_parquet(out / "daily_stats.parquet", stats, STAT_FIELDS)
        (out / "README.md").write_text(card(index, len(offers), len(models), len(providers), args.repo), encoding="utf-8")
        print(f"snapshot {day}: offers={len(offers)} models={len(models)} providers={len(providers)} stats={len(stats)}")
        if args.dry_run:
            return 0
        tok = token()
        if not tok:
            print("::notice::HF_TOKEN secret not set; dataset publishing skipped")
            return 0
        from huggingface_hub import HfApi
        api = HfApi(token=tok)
        api.create_repo(args.repo, repo_type="dataset", exist_ok=True, private=False)
        for attempt in range(5):
            try:
                api.upload_folder(folder_path=str(out), repo_id=args.repo, repo_type="dataset",
                                  commit_message=f"Daily snapshot {day}")
                break
            except Exception as exc:  # noqa: BLE001 - flaky network
                if attempt == 4:
                    raise
                print(f"upload retry {attempt + 1} ({type(exc).__name__})")
                time.sleep(20 * (attempt + 1))
        print(f"published https://huggingface.co/datasets/{args.repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
