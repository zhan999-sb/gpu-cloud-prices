"""Offline IndexNow tests. Inject HTTP and clocks; never ping a real endpoint."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ci.indexnow import DAILY_LIMIT, ENDPOINT, KEY, load_manifest, main, notify, post
from ci.refresh_policy import fingerprint, record_deployed
from pipelines.gpu_prices.store import PriceStore

ORIGIN='https://gpu.example.test'
NOW=datetime(2026,11,14,1,tzinfo=timezone.utc)


class IndexNowTests(unittest.TestCase):
    def setUp(self):
        runtime=Path(__file__).resolve().parents[1]/'runtime'
        runtime.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=runtime,prefix='indexnow-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.manifest=self.root/'indexnow-manifest.json'
        self.receipt=self.root/'deploy-receipt.sqlite'
        (self.root/(KEY+'.txt')).write_bytes((KEY+'\n').encode('utf-8'))
        self.sent=[]
        self.write({'/':'a','/zh/':'b','/data/':'c'})
        result=self.run_notify(now=NOW-timedelta(days=1))
        self.assertEqual(result['status'],'baseline_initialized')
        self.assertFalse(self.sent)

    def write(self,pages):
        self.manifest.write_text(json.dumps({'version':1,'origin':ORIGIN,
            'pages':{ORIGIN+u:h*64 for u,h in pages.items()}}),encoding='utf-8')

    def sender(self,payload):
        self.sent.append(payload)
        return 200

    def run_notify(self,**kwargs):
        return notify(self.manifest,self.receipt,sender=kwargs.pop('sender',self.sender),now=kwargs.pop('now',NOW),**kwargs)

    def test_only_changed_added_and_removed_urls_then_skip(self):
        self.write({'/':'d','/data/':'c','/ja/':'e'})
        result=self.run_notify()
        self.assertEqual(result['attempted'],3)
        payload=self.sent[0]
        self.assertEqual(payload['urlList'],[ORIGIN+'/',ORIGIN+'/ja/',ORIGIN+'/zh/'])
        self.assertEqual(payload['host'],'gpu.example.test')
        self.assertEqual(payload['keyLocation'],ORIGIN+'/'+KEY+'.txt')
        self.assertEqual(self.run_notify()['status'],'unchanged')
        self.assertEqual(len(self.sent),1)

    def test_daily_cap_across_deploys_and_next_utc_day_queue(self):
        self.write({'/page/'+str(i)+'/':'a' for i in range(10_001)})
        result=self.run_notify()
        self.assertEqual(result['attempted'],DAILY_LIMIT)
        self.assertEqual(result['pending'],4)  # 10,001 additions plus 3 deletions
        self.assertEqual(self.run_notify()['status'],'daily_limit')
        self.assertEqual(len(self.sent),1)
        result=self.run_notify(now=NOW+timedelta(days=1))
        self.assertEqual(result['attempted'],4)
        self.assertEqual(self.run_notify(now=NOW+timedelta(days=1))['status'],'unchanged')

    def test_failure_reserves_budget_and_retries_changed_queue(self):
        self.write({'/':'d','/zh/':'b','/data/':'c'})
        def failed(payload):
            raise TimeoutError('offline timeout')
        result=self.run_notify(sender=failed)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['pending'],1)
        with closing(sqlite3.connect(self.receipt)) as conn:
            self.assertEqual(conn.execute('SELECT attempted FROM indexnow_daily WHERE day=?',(NOW.date().isoformat(),)).fetchone()[0],1)
        self.assertEqual(self.run_notify()['attempted'],1)
        self.assertEqual(self.sent[0]['urlList'],[ORIGIN+'/'])
        self.assertEqual(self.run_notify()['status'],'unchanged')

    def test_202_accepted_and_http_error_are_nonfatal(self):
        self.write({'/':'d'})
        self.assertEqual(self.run_notify(sender=lambda p:429)['status'],'failed')
        self.assertEqual(self.run_notify(sender=lambda p:202)['status'],'submitted')

    def test_cold_cache_never_resubmits_site_or_resets_unknown_day_budget(self):
        self.receipt.unlink()
        self.assertEqual(self.run_notify()['status'],'baseline_initialized')
        self.write({'/':'d'})
        self.assertEqual(self.run_notify()['status'],'daily_limit')
        self.assertFalse(self.sent)
        self.assertEqual(self.run_notify(now=NOW+timedelta(days=1))['status'],'submitted')

    def test_refresh_receipt_replacement_preserves_indexnow_state(self):
        with PriceStore(':memory:') as store:
            record_deployed(store,self.receipt,fingerprint(store,now=NOW),now=NOW)
        self.assertEqual(self.run_notify()['status'],'unchanged')
        self.write({'/':'d'})
        self.assertEqual(self.run_notify()['status'],'submitted')
        with closing(sqlite3.connect(self.receipt)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='last_deploy_at'").fetchone()[0],NOW.isoformat())

    def test_validation_rejects_external_urls_hashes_and_bad_key(self):
        for url,digest in [('https://evil.test/','a'*64),(ORIGIN+'/../secret/','a'*64),(ORIGIN+'/?q=1','a'*64),(ORIGIN+'/', 'wrong')]:
            with self.subTest(url=url,digest=digest):
                self.manifest.write_text(json.dumps({'version':1,'origin':ORIGIN,'pages':{url:digest}}),encoding='utf-8')
                with self.assertRaises(ValueError):load_manifest(self.manifest)
        self.write({'/':'d'})
        (self.root/(KEY+'.txt')).write_text('wrong',encoding='utf-8')
        with self.assertRaises(ValueError):self.run_notify()
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(['--manifest',str(self.manifest),'--receipt',str(self.receipt)]),0)
        self.assertEqual(json.loads(output.getvalue())['status'],'skipped_error')
        self.assertFalse(self.sent)

    def test_http_request_and_utc_clock(self):
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
        with patch('ci.indexnow.urlopen',return_value=Response()) as opener:
            self.assertEqual(post({'host':'gpu.example.test','key':KEY,'urlList':[ORIGIN+'/']}),200)
        request=opener.call_args.args[0]
        self.assertEqual(request.full_url,ENDPOINT)
        self.assertEqual(request.method,'POST')
        self.assertEqual(opener.call_args.kwargs['timeout'],20)
        with self.assertRaises(ValueError):self.run_notify(now=NOW.replace(tzinfo=None))
        self.assertEqual(self.run_notify(now=NOW.astimezone(timezone(timedelta(hours=8))))['utc_day'],'2026-11-14')

    def test_workflow_ping_is_after_success_before_receipt_cache(self):
        workflow=(Path(__file__).resolve().parents[2]/'.github/workflows/gpu-site.yml').read_text(encoding='utf-8')
        start=workflow.index('- name: Notify IndexNow')
        end=workflow.index('- name: Persist successful deployment receipt')
        self.assertLess(workflow.index('- name: Record successful deployment'),start)
        self.assertLess(start,end)
        self.assertIn("if: success() && steps.deploy.outcome == 'success'",workflow[start:end])
        self.assertIn('continue-on-error: true',workflow[start:end])
        self.assertIn('--receipt ci/runtime/deploy-receipt.sqlite',workflow[start:end])


if __name__=='__main__':unittest.main()
