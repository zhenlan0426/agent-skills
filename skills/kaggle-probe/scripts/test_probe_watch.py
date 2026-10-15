"""Offline watcher checks: timing provenance, identity matching, and raw evidence."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from probe import decode_observations
from probe_runtime import Codec
from probe_watch import KaggleSource, classify, update_records, validate_mapping, watch_run


MAPPING = {"error_rules": [{"pattern": "unhandled error", "outcome": "exception"},
                           {"pattern": "expected submission file", "outcome": "missing"},
                           {"pattern": "incorrect format", "outcome": "format"}]}
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def submission(index, status="PENDING", **extra):
    return {"ref": index + 100, "description": f"kaggle-probe:test:{index}",
            "date": EPOCH.isoformat(), "status": status, **extra}


class FakeClock:
    def __init__(self):
        self.seconds = 1790

    def now(self):
        return EPOCH + timedelta(seconds=self.seconds)

    def sleep(self, seconds):
        self.seconds += seconds


class FakeSource:
    def __init__(self, batches):
        self.batches = iter(batches)

    def fetch(self, *args):
        return {"submissions": next(self.batches), "pagination_exhausted": True}


class WatchTests(unittest.TestCase):
    def test_mapping_rejects_unknown_ambiguous_and_unscored_outcomes(self):
        codec = Codec()
        validate_mapping(MAPPING, codec)
        self.assertEqual(classify(submission(0, "ERROR", errorDescription="unhandled error"), MAPPING, codec), "exception")
        self.assertEqual(classify(submission(0, "COMPLETE", publicScore="0.05555"), MAPPING, codec), "valid:0")
        for row in (submission(0, "ERROR", errorDescription="GPU quota exceeded"),
                    submission(0, "COMPLETE"), submission(0, "COMPLETE", publicScore="NaN"),
                    submission(0, "TIMEOUT")):
            with self.assertRaises(ValueError):
                classify(row, MAPPING, codec)
        ambiguous = {"error_rules": MAPPING["error_rules"] * 2}
        with self.assertRaises(ValueError):
            classify(submission(0, "ERROR", errorDescription="unhandled error"), ambiguous, codec)
        with self.assertRaises(ValueError):
            validate_mapping(MAPPING, Codec(valid_outcomes=2))
        ranges = {"valid_rules": [{"score_min": "0", "score_max": ".1", "outcome": "valid:0"},
                                  {"score_min": ".2", "score_max": ".3", "outcome": "valid:1"}]}
        self.assertEqual(classify(submission(0, "COMPLETE", publicScore=".25"), ranges,
                                  Codec(valid_outcomes=2)), "valid:1")

    def test_complete_with_error_description_uses_error_rules_and_resume_reclassifies(self):
        codec = Codec(digits=1)
        raw = submission(0, "COMPLETE", errorDescription="incorrect format", publicScore=None)
        self.assertEqual(classify(raw, MAPPING, codec), "format")
        probes = [{"index": 0, "description": "kaggle-probe:test:0"}]
        state = {"records": {}}
        update_records(state, [submission(0)], probes, EPOCH, MAPPING, codec)
        update_records(state, [raw], probes, EPOCH + timedelta(seconds=60), {}, codec)
        row = state["records"]["0"]["observation"]
        self.assertIsNone(row["outcome"])
        self.assertEqual(len(row["issues"]), 1)
        update_records(state, [raw], probes, EPOCH + timedelta(seconds=90), MAPPING, codec)
        self.assertEqual(row["outcome"], "format")
        self.assertNotIn("issues", row)
        self.assertEqual(row["elapsed_seconds"], "60.0")

    def test_already_terminal_has_no_poll_duration_but_explicit_duration_is_usable(self):
        probes = [{"index": 0, "description": "kaggle-probe:test:0"}]
        raw = submission(0, "COMPLETE", publicScore=".1", executionSeconds=1800)
        for mapping, expected in ((MAPPING, None), ({**MAPPING, "duration_field": "executionSeconds"}, "1800")):
            state = {"records": {}}
            update_records(state, [raw], probes, EPOCH + timedelta(seconds=9999), mapping, Codec(digits=1))
            row = state["records"]["0"]["observation"]
            self.assertEqual(row["elapsed_seconds"], expected)
            self.assertEqual(bool(row.get("issues")), expected is None)

    def test_watch_persists_snapshots_and_observations_and_resumes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            probes = [{"index": i, "description": f"kaggle-probe:test:{i}"} for i in range(2)]
            manifest = {"schema_version": 2, "run_id": "test", "competition": "example",
                        "mode": "integer", "codec": asdict(Codec(digits=2)), "probes": probes}
            (root / "manifest.json").write_text(json.dumps(manifest))
            (root / "mapping.json").write_text(json.dumps(MAPPING))
            args = SimpleNamespace(manifest=root / "manifest.json", mapping=root / "mapping.json",
                                   output=root / "watch", poll_seconds=20, timeout_seconds=100,
                                   page_size=100, max_pages=2)
            clock = FakeClock()
            pending = [submission(0), submission(1), submission(7)]
            terminal = [submission(1, "ERROR", errorDescription="incorrect format"),
                        submission(0, "ERROR", errorDescription="expected submission file")]
            result = watch_run(args, source=FakeSource([pending, terminal]), now=clock.now,
                               clock=lambda: clock.seconds, sleep=clock.sleep)
            self.assertEqual(result["terminal_count"], 2)
            self.assertEqual(result["missing_indices"], [])
            observations = json.loads((args.output / "observations.json").read_text())
            self.assertEqual([r["index"] for r in observations], [0, 1])
            self.assertEqual([r["outcome"] for r in observations], ["missing", "format"])
            self.assertEqual(observations[0]["elapsed_seconds"], "1810.0")
            self.assertEqual(observations[0]["elapsed_lower_seconds"], "1790.0")
            decoded = decode_observations(manifest, Codec(digits=2), observations, 0, 30)
            self.assertEqual(decoded["integer"], 232)
            with self.assertRaises(ValueError):
                decode_observations(manifest, Codec(digits=2), observations, 0, 5)
            self.assertEqual(len(list((args.output / "snapshots").glob("*.json"))), 2)
            clock.seconds += 5000
            watch_run(args, source=FakeSource([terminal]), now=clock.now,
                      clock=lambda: clock.seconds, sleep=clock.sleep)
            self.assertEqual(json.loads((args.output / "observations.json").read_text()), observations)
            (root / "mapping.json").write_text(json.dumps({}))
            with self.assertRaisesRegex(ValueError, "different manifest or mapping"):
                watch_run(args, source=FakeSource([]))

    def test_deadline_preserves_partial_results(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            probes = [{"index": i, "description": f"kaggle-probe:test:{i}"} for i in range(2)]
            manifest = {"schema_version": 2, "run_id": "test", "competition": "example",
                        "codec": asdict(Codec(digits=2)), "probes": probes}
            (root / "manifest.json").write_text(json.dumps(manifest))
            (root / "mapping.json").write_text(json.dumps(MAPPING))
            args = SimpleNamespace(manifest=root / "manifest.json", mapping=root / "mapping.json",
                                   output=root / "watch", poll_seconds=20, timeout_seconds=20,
                                   page_size=100, max_pages=2)
            clock = FakeClock()
            rows = [submission(0, "ERROR", errorDescription="unknown failure")]
            result = watch_run(args, source=FakeSource([rows, rows]), now=clock.now,
                               clock=lambda: clock.seconds, sleep=clock.sleep)
            self.assertEqual(result["missing_indices"], [1])
            self.assertEqual(result["undecodable_indices"], [0])

    def test_duplicates_and_changed_refs_are_rejected(self):
        probes = [{"index": 0, "description": "kaggle-probe:test:0"}]
        with self.assertRaisesRegex(ValueError, "Multiple submissions"):
            update_records({"records": {}}, [submission(0), submission(0, ref=900)], probes,
                           EPOCH, MAPPING, Codec(digits=1))
        state = {"records": {}}
        update_records(state, [submission(0)], probes, EPOCH, MAPPING, Codec(digits=1))
        with self.assertRaisesRegex(ValueError, "ref changed"):
            update_records(state, [submission(0, ref=900)], probes, EPOCH, MAPPING, Codec(digits=1))

    def test_sdk_pagination_and_detail_fetch_without_authentication(self):
        class Item:
            def __init__(self, index):
                self.ref = index + 100
                self.description = submission(index)["description"]

        class Detail:
            def to_dict(self, ignore_defaults):
                self_test.assertFalse(ignore_defaults)
                return submission(0)

        class Service:
            def __init__(self):
                self.pages = []

            def list_submissions(self, request):
                self.pages.append((request.page, request.page_token))
                return SimpleNamespace(submissions=[Item(9)] if len(self.pages) == 1 else [Item(0)],
                                       next_page_token="next" if len(self.pages) == 1 else "")

            def get_submission(self, request):
                self_test.assertEqual(request.ref, 100)
                return Detail()

        self_test = self
        service = Service()
        client = SimpleNamespace(competitions=SimpleNamespace(competition_api_client=service))
        from contextlib import nullcontext
        source = KaggleSource.__new__(KaggleSource)
        source.api = SimpleNamespace(build_kaggle_client=lambda: nullcontext(client))
        module = SimpleNamespace(ApiGetSubmissionRequest=SimpleNamespace, ApiListSubmissionsRequest=SimpleNamespace)
        with patch.dict("sys.modules", {"kagglesdk.competitions.types.competition_api_service": module}):
            result = source.fetch("example", {submission(0)["description"]}, 2, 3)
        self.assertEqual(service.pages, [(0, ""), (-1, "next")])
        self.assertTrue(result["pagination_exhausted"])
        self.assertEqual(result["submissions"][0]["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
