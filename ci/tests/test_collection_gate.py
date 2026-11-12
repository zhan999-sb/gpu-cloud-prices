from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from ci.collection_gate import classify
from ci.refresh_policy import decide, fingerprint, prepare_save, record_deployed
from pipelines.gpu_prices import run
from pipelines.gpu_prices.http import HttpClient
from pipelines.gpu_prices.providers.clore_collector import CloreCollector
from pipelines.gpu_prices.providers.page_collector import PageCollector
from pipelines.gpu_prices.schema import Offer
from pipelines.gpu_prices.store import PriceStore


NOW = datetime(2026, 11, 12, 17, 30, tzinfo=timezone.utc)
CI = Path(__file__).resolve().parents[1]


def result(status="complete", records=3):
    return {"status": status, "records": records}


def skipped(**changes):
    return {"status": "skipped", "pages": 0, "records": 0, "models": [],
            "reason": "page fetched within 60 minutes", "checked_at": NOW.isoformat(),
            "previous_run": {"status": "complete", "records": 3,
                             "finished_at": (NOW - timedelta(minutes=15)).isoformat()},
            **changes}


class CollectionGateTests(unittest.TestCase):
    def test_success_and_partial_failure(self):
        self.assertTrue(classify({"collections": {"azure": result()}}, 0)[0])
        self.assertTrue(classify({"collections": {
            "azure": result(), "linode": result("failed", 0)}}, 1)[0])

    def test_all_failed_or_empty_blocks_deploy(self):
        for providers, code in [({"azure": result("failed", 0)}, 1),
                                ({"azure": result(records=0)}, 0)]:
            with self.subTest(providers=providers):
                self.assertFalse(classify({"collections": providers}, code)[0])

    def test_fatal_or_unknown_results_cannot_be_masked_by_one_success(self):
        good = {"azure": result()}
        cases = [({"collections": good, "error": "database write failed"}, 1),
                 ({"collections": good}, 2),
                 ({"collections": good}, 1),
                 ({"collection": result()}, 0),
                 ({"collections": {}}, 0),
                 ({"collections": {**good, "new": result("limited")}}, 0),
                 ({"collections": {**good, "new": result("failed", 0)}}, 0),
                 ({"collections": {"azure": result(records="3")}}, 0),
                 (None, 1)]
        for summary, code in cases:
            with self.subTest(summary=summary, code=code):
                self.assertFalse(classify(summary, code)[0])

    def test_throttled_page_providers_and_successful_apis_pass(self):
        providers = {"azure": result(), "linode": result(),
                     **{name: skipped() for name in
                        ("featurize", "nebius", "lambda", "paperspace", "baidu_gpu",
                         "anygpu", "ai_galaxy", "massedcompute", "clore")}}
        allowed, message = classify({"collections": providers}, 0)
        self.assertTrue(allowed)
        self.assertIn("fresh successful data retained", message)

    def test_all_throttled_with_fresh_successes_pass(self):
        # records=0 means nothing fetched THIS time, not an empty cached batch.
        allowed, message = classify({"collections": {"nebius": skipped(), "lambda": skipped()}}, 0)
        self.assertTrue(allowed)
        self.assertIn("no new observations", message)

    def test_clore_401_is_an_ordinary_partial_failure(self):
        clore = {**result("failed", 0), "pages": 0, "models": [],
                 "error": "HTTP Error 401: set CLORE_API_KEY secret to authenticate the Clore marketplace"}
        allowed, message = classify({"collections": {"azure": result(), "clore": clore}}, 1)
        self.assertTrue(allowed)
        self.assertIn("Partial collection", message)
        self.assertIn("clore", message)

    def test_all_providers_failed(self):
        providers = {name: result("failed", 0) for name in ("azure", "linode", "clore", "nebius")}
        self.assertFalse(classify({"collections": providers}, 1)[0])

    def test_unknown_status_and_limited_batches_still_fail_closed(self):
        # limited still means truncated pagination, not the hourly skipped state.
        for status in ("unknown", "throttled", "limited", None, []):
            with self.subTest(status=status):
                self.assertFalse(classify({"collections": {"azure": result(), "nebius": result(status)}}, 0)[0])

    def test_display_only_success_cannot_mask_all_api_failures(self):
        self.assertFalse(classify({"collections": {"azure": result("failed", 0), "nebius": result()}}, 1)[0])

    def test_skips_with_no_successful_cache_follow_partial_failure_policy(self):
        for previous in (None, {"status": "failed", "records": 0},
                         {"status": "complete", "records": 0, "finished_at": NOW.isoformat()},
                         {"status": "complete", "records": 3,
                          "finished_at": (NOW - timedelta(hours=1)).isoformat()}):
            with self.subTest(previous=previous):
                providers = {"nebius": skipped(previous_run=previous)}
                self.assertFalse(classify({"collections": providers}, 0)[0])
                allowed, message = classify({"collections": {"azure": result(), **providers}}, 0)
                self.assertTrue(allowed)
                self.assertIn("failed/stale/empty providers: nebius", message)

    def test_invalid_skip_evidence_fails_closed(self):
        cases = [skipped(reason="disabled"), skipped(pages=1), skipped(pages=False),
                 skipped(records=1), skipped(checked_at="not-a-timestamp"),
                 skipped(checked_at=NOW.replace(tzinfo=None).isoformat()),
                 skipped(previous_run={"status": "unknown", "records": 3}),
                 skipped(previous_run={"status": "limited", "records": 3}),
                 skipped(previous_run={"status": "complete", "records": "3"}),
                 skipped(previous_run={"status": "complete", "records": 3}),
                 skipped(previous_run={"status": "complete", "records": 3, "finished_at": None})]
        for entry in cases:
            with self.subTest(entry=entry):
                self.assertFalse(classify({"collections": {"azure": result(), "nebius": entry}}, 0)[0])

    def test_skip_does_not_mask_exit_code_disagreement(self):
        for providers, code in [({"clore": skipped()}, 1),
                                ({"azure": result(), "clore": result("failed", 0), "nebius": skipped()}, 0)]:
            with self.subTest(providers=providers, code=code):
                self.assertFalse(classify({"collections": providers}, code)[0])


class CollectionGateIntegrationTests(unittest.TestCase):
    """Exercise the real CLI/SQLite contract without service IO or the runtime DB."""

    def setUp(self):
        runtime = CI / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="collection-gate-tests-", dir=runtime)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "prices.sqlite"

    def seed_success(self, provider, stamp):
        provenance = ({"price_per_day": 72, "source_billing_unit": "instance-day",
                       "price_basis": "provider_usd_conversion"} if provider == "clore" else {})
        with PriceStore(self.db) as store:
            store.upsert([Offer(provider=provider, sku="test-h100", gpu_model="H100 SXM 80GB",
                                gpu_count=1, vcpu=None, ram_gb=None, region=None,
                                price_type="on_demand", currency="USD", price_per_hour=3,
                                price_per_gpu_hour=3, source_url="https://nebius.com/prices",
                                collected_at=stamp.isoformat(),
                                tier="A" if provider in {"azure", "clore"} else "B",
                                export_allowed=provider in {"azure", "clore"}, **provenance)])
            with patch("pipelines.gpu_prices.store.utc_now", return_value=stamp.isoformat()):
                store.record_run(provider=provider, started_at=(stamp - timedelta(seconds=1)).isoformat(),
                                 status="complete", pages=1, records=1)
            self.assertTrue(store.claim_page_fetch(provider, stamp.isoformat()))

    def collect(self, providers, *, at=NOW, clore_client=None):
        stamp = at.isoformat()
        output = StringIO()
        args = ["collect", "--db", str(self.db)]
        for provider in providers:
            args.extend(["--provider", provider])
        with ExitStack() as stack:
            stack.enter_context(patch.object(run, "configure_logging"))
            stack.enter_context(patch.object(run, "utc_now", return_value=stamp))
            stack.enter_context(patch("pipelines.gpu_prices.store.utc_now", return_value=stamp))
            stack.enter_context(patch.object(run, "PageCollector", side_effect=lambda name, **kw:
                PageCollector(name, client=HttpClient(opener=self.no_network), clock=lambda: stamp, **kw)))
            stack.enter_context(patch.object(run, "CloreCollector", side_effect=lambda **kw:
                CloreCollector(client=clore_client or HttpClient(opener=self.no_network), clock=lambda: stamp, **kw)))
            stack.enter_context(redirect_stdout(output))
            code = run.main(args)
        return json.loads(output.getvalue()), code

    def no_network(self, *args, **kwargs):
        raise AssertionError("Hourly throttle must prevent service IO")

    def gate_cli(self, summary, code):
        report = self.root / "collect.json"
        report.write_text(json.dumps(summary), encoding="utf-8")
        return subprocess.run([sys.executable, "-B", str(CI / "collection_gate.py"), str(report), str(code)],
                              capture_output=True, text=True, check=False)

    def test_all_throttled_cli_preserves_current_data_and_does_not_force_redeploy(self):
        for provider in ("nebius", "lambda"):
            self.seed_success(provider, NOW - timedelta(minutes=15))
        with PriceStore(self.db) as store:
            prepare_save(store, now=NOW - timedelta(minutes=10))
            digest = fingerprint(store, now=NOW)
            record_deployed(store, self.root / "receipt.sqlite", digest, now=NOW - timedelta(minutes=10))
            before = store.connection.execute("SELECT * FROM collection_runs ORDER BY id").fetchall()
        summary, code = self.collect(["nebius", "lambda"])
        self.assertEqual(code, 0)
        self.assertTrue(all(entry["status"] == "skipped" for entry in summary["collections"].values()))
        proc = self.gate_cli(summary, code)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::notice::All providers hourly throttled", proc.stdout)
        with PriceStore(self.db) as store:
            self.assertEqual(before, store.connection.execute("SELECT * FROM collection_runs ORDER BY id").fetchall())
            self.assertEqual({row.provider for row in store.current_prices(NOW.date().isoformat(), NOW.isoformat())},
                             {"nebius", "lambda"})
            self.assertEqual(fingerprint(store, now=NOW), digest)
            decision = decide(store, now=NOW)
            self.assertFalse(decision["build"])
            self.assertFalse(decision["save"])

    def test_clore_401_cli_emits_notice_and_keeps_only_successful_provider_current(self):
        old = NOW - timedelta(hours=2)
        for provider in ("azure", "clore"):
            self.seed_success(provider, old)
        with PriceStore(self.db) as store:
            # Azure succeeds in the same collection; isolate the Clore HTTP path.
            row = next(row for row in store.latest_prices() if row.provider == "azure")

        class AzureSuccess:
            complete, pages_fetched, raw_items = True, 1, 1

            def collect(self, *, limit_pages=None):
                return [replace(row, collected_at=NOW.isoformat())]

        def unauthorized(req, timeout):
            raise HTTPError(req.full_url, 401, "unauthorized", {}, BytesIO(b"private body"))

        with patch.dict(os.environ, {"CLORE_API_KEY": ""}), patch.object(run, "AzureCollector", AzureSuccess):
            summary, code = self.collect(["azure", "clore"], clore_client=HttpClient(opener=unauthorized))
        self.assertEqual(code, 1)
        self.assertEqual(summary["collections"]["clore"]["status"], "failed")
        self.assertIn("401", summary["collections"]["clore"]["error"])
        proc = self.gate_cli(summary, code)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::notice::Partial collection", proc.stdout)
        self.assertIn("clore", proc.stdout)
        with PriceStore(self.db) as store:
            self.assertEqual({row.provider for row in store.current_prices(NOW.date().isoformat(), NOW.isoformat())}, {"azure"})
            self.assertEqual({row.provider for row in store.latest_prices()}, {"azure", "clore"})
        # The failed request consumes the budget; the next skip must not revive it.
        next_summary, next_code = self.collect(["clore"], at=NOW + timedelta(minutes=15))
        self.assertEqual(next_code, 0)
        self.assertEqual(next_summary["collections"]["clore"]["previous_run"]["status"], "failed")
        self.assertFalse(classify(next_summary, next_code)[0])
        self.assertTrue(classify({"collections": {"azure": result(), **next_summary["collections"]}}, 0)[0])

    def test_unknown_status_cli_fails_with_error(self):
        proc = self.gate_cli({"collections": {"azure": result(), "new": result("unknown")}}, 0)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error::Unknown", proc.stdout)
