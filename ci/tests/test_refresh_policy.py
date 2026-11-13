"""Refresh policy tests use injected UTC clocks and disposable SQLite files."""

from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ci.refresh_policy import (decide, fingerprint, main, prepare_save,
                               record_deployed, restore_receipt)
from ci.sqlite_snapshot import snapshot
from pipelines.gpu_prices.schema import Offer
from pipelines.gpu_prices.store import PriceStore


NOW = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)


def offer(**changes):
    return replace(Offer(provider='azure', sku='test-h100', gpu_model='H100 SXM 80GB',
                         gpu_count=8, vcpu=96, ram_gb=1900, region='eastus',
                         price_type='on_demand', currency='USD', price_per_hour=24,
                         price_per_gpu_hour=3, source_url='https://prices.azure.com/api/retail/prices',
                         collected_at=NOW.isoformat()), **changes)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        runtime = Path(__file__).resolve().parents[1] / 'runtime'
        runtime.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=runtime, prefix='refresh-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.receipt = self.root / 'deploy.sqlite'
        self.db = self.root / 'prices.sqlite'
        self.store = PriceStore(self.db)
        self.addCleanup(self.store.close)
        self.store.upsert([offer()])

    def saved_and_deployed(self):
        self.assertTrue(prepare_save(self.store, now=NOW))
        digest = fingerprint(self.store, now=NOW)
        record_deployed(self.store, self.receipt, digest, now=NOW)
        return digest

    def test_first_run_then_unchanged_and_six_hour_boundary(self):
        first = decide(self.store, now=NOW)
        self.assertTrue(first['build'] and first['save'] and first['changed'])
        self.saved_and_deployed()
        for delta, expected in [(timedelta(minutes=15), False),
                                (timedelta(hours=6) - timedelta(microseconds=1), False),
                                (timedelta(hours=6), True)]:
            with self.subTest(delta=delta):
                result = decide(self.store, now=NOW + delta)
                self.assertEqual(result['build'], expected)
                self.assertFalse(result['save'])

    def test_fingerprint_excludes_observation_times_and_order(self):
        second = offer(sku='other', region='westus', price_per_hour=32, price_per_gpu_hour=4)
        self.store.upsert([second])
        before = fingerprint(self.store, now=NOW)
        with PriceStore(':memory:') as other:
            other.upsert([replace(second, collected_at='2026-11-12T08:15:00Z'),
                          offer(collected_at='2026-11-12T08:15:00Z',
                                availability_observed_at='2026-11-12T08:15:00Z', gpu_count=8.0)])
            self.assertEqual(before, fingerprint(other, now=NOW + timedelta(minutes=15)))
        self.assertEqual(before, fingerprint(self.store, now=NOW.astimezone(timezone(timedelta(hours=8)))))

    def test_every_price_contract_field_changes_digest(self):
        before = fingerprint(self.store, now=NOW)
        cases = dict(provider='linode', sku='changed', region='westus', price_type='spot',
                     price_per_hour=32, price_per_gpu_hour=4, currency='EUR', tier='B',
                     effective_start_date='2026-01-01T00:00:00Z',
                     effective_from='2026-02-01T00:00:00Z', effective_to='2027-01-01T00:00:00Z',
                     original_price=25, original_currency='EUR', fx_rate=1.1,
                     price_scope='per_gpu', gpu_fraction=0.5)
        for key, value in cases.items():
            with self.subTest(field=key), PriceStore(':memory:') as other:
                changes = {key: value}
                if key == 'tier':
                    changes['export_allowed'] = False
                if key == 'price_per_hour':
                    changes['price_per_gpu_hour'] = value / 8
                if key == 'price_per_gpu_hour':
                    changes['price_per_hour'] = value * 8
                if key == 'price_scope':
                    changes['price_per_gpu_hour'] = 24
                if key == 'original_currency':
                    changes.update(original_price=24, fx_rate=1, fx_date='2026-11-12',
                                   fx_source='https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml')
                other.upsert([offer(**changes)])
                self.assertNotEqual(before, fingerprint(other, now=NOW))

    def test_effective_dates_canonicalize_but_expiry_changes_population(self):
        with PriceStore(':memory:') as other:
            self.store.upsert([offer(collected_at='2026-11-12T08:01:00Z',
                                    effective_to='2026-11-12T09:00:00Z')])
            other.upsert([offer(effective_to='2026-11-12T09:00:00.000000+00:00')])
            self.assertEqual(fingerprint(self.store, now=NOW), fingerprint(other, now=NOW))
            self.assertNotEqual(fingerprint(other, now=NOW), fingerprint(other, now=NOW + timedelta(hours=1)))

    def test_current_snapshot_removes_missing_keys_and_failed_providers(self):
        self.store.upsert([offer(sku='gone')])
        with patch('pipelines.gpu_prices.store.utc_now', return_value=NOW.isoformat()) as clock:
            self.store.record_run(provider='azure', started_at=(NOW - timedelta(minutes=1)).isoformat(),
                                  status='complete', pages=1, records=2)
            before = fingerprint(self.store, now=NOW)  # Warm the observation cache.
            self.assertEqual({o.sku for o in self.store.current_prices(NOW.date().isoformat(), NOW.isoformat())},
                             {'test-h100', 'gone'})

            later = NOW + timedelta(minutes=15)
            # Replay the collector's public API sequence. Raw SQL offer edits
            # bypass the cache invalidation performed by upsert().
            self.store.upsert([offer(collected_at=later.isoformat())])
            clock.return_value = later.isoformat()
            self.store.record_run(provider='azure', started_at=(later - timedelta(minutes=1)).isoformat(),
                                  status='complete', pages=1, records=1)
            active = fingerprint(self.store, now=later)
            self.assertNotEqual(before, active)
            self.assertEqual([o.sku for o in self.store.current_prices(later.date().isoformat(), later.isoformat())],
                             ['test-h100'])
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM offers').fetchone()[0], 2)
            with PriceStore(self.db, read_only=True) as restored:
                self.assertEqual(active, fingerprint(restored, now=later))

            failed_at = later + timedelta(minutes=15)
            clock.return_value = failed_at.isoformat()
            self.store.record_run(provider='azure', started_at=(failed_at - timedelta(minutes=1)).isoformat(),
                                  status='failed', pages=0, records=0)
            self.assertNotEqual(active, fingerprint(self.store, now=failed_at))
            self.assertEqual(self.store.current_prices(failed_at.date().isoformat(), failed_at.isoformat()), [])
            self.assertEqual([r[0] for r in self.store.connection.execute('SELECT status FROM collection_runs ORDER BY id')],
                             ['complete', 'failed'])

    def test_visible_population_changes_require_save_and_build(self):
        # Unchanged prices must still refresh when a complete run loses one or
        # all offers, or a failure hides a provider. Other providers stay visible.
        cases = [('missing_offer', 'complete', [offer()]),
                 ('empty_complete_run', 'complete', []),
                 ('failed_provider', 'failed', [])]
        unrelated = offer(provider='linode', sku='unrelated')
        for name, status, remaining in cases:
            with self.subTest(case=name), PriceStore(':memory:') as store, PriceStore(':memory:') as expected:
                store.upsert([offer(), offer(sku='gone'), unrelated])
                with patch('pipelines.gpu_prices.store.utc_now', return_value=NOW.isoformat()) as clock:
                    for provider, records in [('azure', 2), ('linode', 1)]:
                        store.record_run(provider=provider, started_at=(NOW - timedelta(minutes=1)).isoformat(),
                                         status='complete', pages=1, records=records)
                    prepare_save(store, now=NOW)
                    before = fingerprint(store, now=NOW)
                    record_deployed(store, self.receipt, before, now=NOW)
                    self.assertFalse(decide(store, now=NOW)['build'])
                    self.assertFalse(decide(store, now=NOW)['save'])

                    later = NOW + timedelta(minutes=15)
                    store.upsert([replace(row, collected_at=later.isoformat()) for row in remaining])
                    clock.return_value = later.isoformat()
                    store.record_run(provider='azure', started_at=(later - timedelta(minutes=1)).isoformat(),
                                     status=status, pages=1 if status == 'complete' else 0, records=len(remaining))

                expected.upsert(remaining + [unrelated])
                result = decide(store, now=later)
                self.assertEqual({(o.provider, o.sku) for o in store.current_prices(later.date().isoformat(), later.isoformat())},
                                 {(o.provider, o.sku) for o in remaining + [unrelated]})
                self.assertEqual(result['fingerprint'], fingerprint(expected, now=later))
                self.assertNotEqual(before, result['fingerprint'])
                self.assertTrue(result['changed'] and result['save'] and result['build'])
                self.assertFalse(result['new_utc_day'])

    def test_price_change_or_failed_deploy_is_never_suppressed(self):
        self.saved_and_deployed()
        self.store.upsert([offer(collected_at='2026-11-12T08:15:00Z', price_per_hour=32, price_per_gpu_hour=4)])
        decision = decide(self.store, now=NOW + timedelta(minutes=15))
        self.assertTrue(decision['save'] and decision['build'])
        # History can be saved after a build/audit/deploy failure. That must not
        # count as a successful deploy or suppress the next build attempt.
        prepare_save(self.store, now=NOW + timedelta(minutes=15))
        self.assertFalse(decide(self.store, now=NOW + timedelta(minutes=30))['save'])
        self.assertTrue(decide(self.store, now=NOW + timedelta(minutes=30))['build'])

    def test_first_save_new_utc_day_retains_daily_history(self):
        late = NOW.replace(hour=23, minute=50)
        self.assertTrue(prepare_save(self.store, now=late))
        day2 = NOW + timedelta(days=1)
        self.store.upsert([offer(collected_at=day2.isoformat())])
        self.assertEqual(fingerprint(self.store, now=late), fingerprint(self.store, now=day2))
        self.assertTrue(prepare_save(self.store, now=day2))
        self.assertFalse(prepare_save(self.store, now=day2 + timedelta(minutes=15)))
        target = self.root / 'daily-backup.sqlite'
        snapshot(self.db, target)
        with PriceStore(target, read_only=True) as restored:
            self.assertEqual([p['day'] for p in restored.daily_series('H100 SXM 80GB')],
                             ['2026-11-12', '2026-11-13'])

    def test_freshness_only_deploy_receipt_survives_fresh_checkout_without_data_save(self):
        digest = self.saved_and_deployed()
        branch = self.root / 'data-branch.sqlite'
        snapshot(self.db, branch)
        later = NOW + timedelta(hours=6)
        self.assertTrue(decide(self.store, now=later)['build'])
        record_deployed(self.store, self.receipt, digest, now=later)
        self.assertFalse(prepare_save(self.store, now=later))
        with PriceStore(branch) as restored:
            restore_receipt(restored, self.receipt, now=later + timedelta(minutes=15))
            result = decide(restored, now=later + timedelta(minutes=15))
            self.assertFalse(result['build'] or result['save'])
            self.assertEqual(dict(restored.connection.execute('SELECT key,value FROM meta'))['last_deploy_at'], later.isoformat())
        with closing(sqlite3.connect(self.receipt)) as conn:
            self.assertEqual([r[0] for r in conn.execute('SELECT key FROM meta ORDER BY key')],
                             ['deployed_fingerprint', 'last_deploy_at'])

    def test_missing_corrupt_stale_or_future_receipt_cannot_hide_needed_build(self):
        for kind in ('missing', 'corrupt', 'future'):
            with self.subTest(kind=kind):
                if self.receipt.exists():
                    self.receipt.unlink()
                if kind == 'corrupt':
                    self.receipt.write_bytes(b'bad cache')
                elif kind == 'future':
                    with closing(sqlite3.connect(self.receipt)) as conn, conn:
                        conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
                        conn.executemany('INSERT INTO meta VALUES (?,?)',
                                         [('deployed_fingerprint',fingerprint(self.store, now=NOW)),
                                          ('last_deploy_at',(NOW + timedelta(days=1)).isoformat())])
                restore_receipt(self.store, self.receipt, now=NOW)
                self.assertTrue(decide(self.store, now=NOW)['build'])
        self.receipt.unlink()
        digest = self.saved_and_deployed()
        record_deployed(self.store, self.root / 'newer.sqlite', digest, now=NOW + timedelta(hours=1))
        restore_receipt(self.store, self.receipt, now=NOW + timedelta(hours=2))
        meta = dict(self.store.connection.execute('SELECT key,value FROM meta'))
        self.assertEqual(meta['last_deploy_at'], (NOW + timedelta(hours=1)).isoformat())

    def test_wrong_snapshot_cannot_be_recorded_as_success(self):
        with self.assertRaises(ValueError):
            record_deployed(self.store, self.receipt, '0' * 64, now=NOW)
        self.assertFalse(self.receipt.exists())

    def test_success_replaces_corrupt_cached_receipt(self):
        self.receipt.write_bytes(b'corrupt cache')
        record_deployed(self.store, self.receipt, fingerprint(self.store, now=NOW), now=NOW)
        with closing(sqlite3.connect(self.receipt)) as conn:
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_deploy_crossing_midnight_records_the_built_snapshot(self):
        start = NOW.replace(hour=23, minute=59)
        finish = start + timedelta(minutes=2)
        decision = decide(self.store, now=start)
        record_deployed(self.store, self.receipt, decision['fingerprint'],
                        now=finish, snapshot_at=decision['snapshot_at'])
        meta = dict(self.store.connection.execute('SELECT key,value FROM meta'))
        self.assertEqual(meta['last_deploy_at'], finish.isoformat())
        self.assertTrue(decide(self.store, now=finish)['build'])

    def test_cli_outputs_boolean_actions_values(self):
        output = self.root / 'github-output'
        with redirect_stdout(StringIO()) as stdout:
            main(['decide', '--db', str(self.db), '--github-output', str(output)])
        result = json.loads(stdout.getvalue())
        self.assertIn('build=true', output.read_text(encoding='utf-8'))
        self.assertIn('fingerprint=' + result['fingerprint'], output.read_text(encoding='utf-8'))
        self.assertRegex(result['fingerprint'], r'^[0-9a-f]{64}$')


class WorkflowTests(unittest.TestCase):
    def test_clean_checkout_basetemp_parents_and_refresh_gates(self):
        root = Path(__file__).resolve().parents[2]
        for filename in ('pr-checks.yml', 'gpu-site.yml'):
            workflow = (root / '.github/workflows' / filename).read_text(encoding='utf-8')
            step = workflow[:workflow.index('--basetemp ci/runtime/pipeline-tests')]
            self.assertIn('mkdir -p ci/runtime\n', step)
            self.assertIn('python -m pytest ci/tests tests/test_gpu_*.py ', step)
        production = (root / '.github/workflows/gpu-site.yml').read_text(encoding='utf-8')
        self.assertIn("cron: '*/15 * * * *'", production)
        # Scheduled runs follow the price/deploy-age policy; code pushes and manual runs always rebuild.
        self.assertEqual(production.count(
            "if: success() && (steps.refresh.outputs.build == 'true' || github.event_name != 'schedule')"), 2)
        self.assertNotIn("if: success() && steps.refresh.outputs.build == 'true'\n", production)
        self.assertIn('ci.refresh_policy prepare-save', production)
        self.assertIn('actions/cache/save@', production)


if __name__ == '__main__':
    unittest.main()
