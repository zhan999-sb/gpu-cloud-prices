import unittest

from ci.collection_gate import classify


def result(status="complete", records=3):
    return {"status": status, "records": records}


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
