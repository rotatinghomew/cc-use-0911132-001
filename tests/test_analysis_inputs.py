import json
import unittest
from pathlib import Path


class AnalysisInputsTests(unittest.TestCase):
    def load_data(self):
        return json.loads((Path(__file__).resolve().parents[1] / "fixtures/analysis/input.json").read_text())

    def test_stock_gap(self):
        data = self.load_data()
        self.assertTrue(any(not row['available'] for row in data['availability']))

    def test_late_return(self):
        data = self.load_data()
        self.assertTrue(any(row['available_at'] > data['as_of'] for row in data['revisions']))

    def test_buyer_concentration(self):
        data = self.load_data()
        self.assertTrue(sum(row['buyer_id'] == 'buyer-wholesale' for row in data['orders']) == 22)

    def test_source_versions_and_cutoff(self):
        data = self.load_data()
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["source_versions"])
        self.assertIn("+00:00", data["as_of"])
