import json
import tempfile
import unittest
from pathlib import Path

from evidence_desk.cli import main
from evidence_desk.core import InputError, load_records, priority, select, to_markdown

RECORDS = [
    {"id": "A-2", "status": "open", "severity": "high", "category": "maintenance", "title": "Two",
     "summary": "s", "source_url": "https://example.com/2", "severity_weight": 3},
    {"id": "A-1", "status": "closed", "severity": "medium", "category": "safety", "title": "One",
     "summary": "s", "source_url": "https://example.com/1", "severity_weight": 2},
    {"id": "A-3", "status": "closed", "severity": "medium", "category": "safety", "title": "Three",
     "summary": "s", "source_url": "https://example.com/3", "severity_weight": 2},
]


def write(directory: Path, content) -> Path:
    path = directory / "input.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


class EvidenceDeskTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def test_priority_adds_open_and_maintenance(self):
        self.assertEqual(priority(RECORDS[0]), 5)
        self.assertEqual(priority(RECORDS[1]), 2)

    def test_filters_combine_with_and(self):
        chosen = select(RECORDS, status="open", severity="high", category="maintenance")
        self.assertEqual([item["id"] for item in chosen], ["A-2"])
        self.assertEqual(select(RECORDS, status="open", category="safety"), [])

    def test_sorting_is_priority_then_id(self):
        self.assertEqual([item["id"] for item in select(RECORDS)], ["A-2", "A-1", "A-3"])

    def test_source_urls_are_kept(self):
        chosen = select(RECORDS)
        self.assertEqual({item["source_url"] for item in chosen}, {item["source_url"] for item in RECORDS})
        self.assertIn("https://example.com/2", to_markdown(chosen))

    def test_malformed_json_is_an_input_error(self):
        with self.assertRaises(InputError):
            load_records(write(self.dir, "{bad"))

    def test_missing_field_is_an_input_error(self):
        broken = [{key: value for key, value in RECORDS[0].items() if key != "title"}]
        with self.assertRaises(InputError):
            load_records(write(self.dir, broken))

    def test_empty_result_writes_a_clear_message(self):
        source = write(self.dir, RECORDS)
        output = self.dir / "out.md"
        code = main(["--input", str(source), "--status", "open", "--severity", "low", "--output", str(output)])
        self.assertEqual(code, 0)
        self.assertIn("No records match", output.read_text(encoding="utf-8"))

    def test_bad_input_exits_non_zero(self):
        code = main(["--input", str(write(self.dir, "{bad")), "--output", str(self.dir / "out.json")])
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
