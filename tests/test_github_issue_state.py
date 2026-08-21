from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import io
from pathlib import Path
import unittest
from unittest import mock
import urllib.error
from contextlib import redirect_stdout

from tools import github_issue_state as state
from tools import release_gate


SOURCE_SHA = "a" * 40
NOW = datetime(2026, 8, 21, 3, 0, tzinfo=timezone.utc)


def _snapshot(*, issue_overrides: dict[int, dict[str, object]] | None = None, retrieved_at: datetime = NOW) -> dict[str, object]:
    overrides = issue_overrides or {}
    issues: list[dict[str, object]] = []
    for number in state.AUDIT_ISSUE_NUMBERS:
        item: dict[str, object] = {
            "issueNumber": number,
            "state": "closed",
            "stateReason": "completed",
            "closedAt": state.format_datetime(retrieved_at - timedelta(minutes=5)),
            "updatedAt": state.format_datetime(retrieved_at - timedelta(minutes=5)),
            "url": f"https://github.com/{state.REPOSITORY}/issues/{number}",
        }
        item.update(overrides.get(number, {}))
        record = {key: item[key] for key in ("issueNumber", "state", "stateReason", "closedAt", "updatedAt", "url")}
        item["etag"] = None
        item["responseDigest"] = state.sha256_json(record)
        issues.append(item)
    snapshot: dict[str, object] = {
        "schema": state.SCHEMA_NAME,
        "version": state.VERSION,
        "repository": state.REPOSITORY,
        "sourceSha": SOURCE_SHA,
        "retrievedAt": state.format_datetime(retrieved_at),
        "expiresAt": state.format_datetime(retrieved_at + timedelta(seconds=state.DEFAULT_MAX_AGE_SECONDS)),
        "maxAgeSeconds": state.DEFAULT_MAX_AGE_SECONDS,
        "retrievedBy": "test",
        "retrieval": {
            "method": "GET",
            "apiBaseUrl": "https://api.github.com",
            "apiVersion": state.API_VERSION,
            "userAgent": state.USER_AGENT,
        },
        "ci": {
            "provider": "local",
            "repository": state.REPOSITORY,
            "sourceSha": SOURCE_SHA,
            "runId": "local",
            "attempt": 1,
            "job": "test",
        },
        "issues": issues,
    }
    snapshot["responseDigest"] = state.sha256_json(state._response_records(snapshot))
    snapshot["snapshotDigest"] = state.sha256_json(snapshot)
    return snapshot


class _Response:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.headers = {"ETag": '"test-etag"'}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class GithubIssueStateTests(unittest.TestCase):
    def test_api_unavailable_fails_closed(self) -> None:
        def unavailable(*_: object, **__: object) -> object:
            raise urllib.error.URLError("offline")

        with self.assertRaises(state.IssueStateError) as raised:
            state.fetch_live_issue_state(source_sha=SOURCE_SHA, now=NOW, opener=unavailable)
        self.assertEqual(raised.exception.code, "GITHUB_API_UNAVAILABLE")

    def test_open_and_reopened_issue_block_the_boundary(self) -> None:
        snapshot = _snapshot(issue_overrides={90: {"state": "open", "stateReason": "reopened", "closedAt": None}})
        boundary = state.derive_release_boundary(snapshot)
        self.assertTrue(boundary["releaseBlocked"])
        self.assertIn("GITHUB_ISSUE_REOPENED", {item["code"] for item in boundary["blockingIssues"]})

    def test_stale_snapshot_is_rejected(self) -> None:
        snapshot = _snapshot()
        with self.assertRaises(state.IssueStateError) as raised:
            state.validate_snapshot(snapshot, expected_source_sha=SOURCE_SHA, now=NOW + timedelta(seconds=state.DEFAULT_MAX_AGE_SECONDS + 1))
        self.assertEqual(raised.exception.code, "ISSUE_SNAPSHOT_STALE")

    def test_wrong_repository_sha_and_attempt_are_rejected(self) -> None:
        snapshot = _snapshot()
        with self.assertRaises(state.IssueStateError) as repo_error:
            state.validate_snapshot(snapshot, expected_repository="someone/else", expected_source_sha=SOURCE_SHA, now=NOW)
        self.assertEqual(repo_error.exception.code, "ISSUE_STATE_REPOSITORY_MISMATCH")
        with self.assertRaises(state.IssueStateError) as sha_error:
            state.validate_snapshot(snapshot, expected_source_sha="b" * 40, now=NOW)
        self.assertEqual(sha_error.exception.code, "ISSUE_STATE_SHA_MISMATCH")
        with self.assertRaises(state.IssueStateError) as attempt_error:
            state.validate_snapshot(snapshot, expected_source_sha=SOURCE_SHA, expected_attempt=2, now=NOW)
        self.assertEqual(attempt_error.exception.code, "ISSUE_STATE_ATTEMPT_MISMATCH")

    def test_static_completion_contradiction_cannot_override_open_live_state(self) -> None:
        snapshot = _snapshot(issue_overrides={91: {"state": "open", "stateReason": None, "closedAt": None}})
        with mock.patch.object(release_gate, "current_head", return_value=SOURCE_SHA), mock.patch.object(state, "_now", return_value=NOW):
            with self.assertRaises(release_gate.GateError) as raised:
                release_gate.check_audit_recovery_release_boundary(issue_state=snapshot)
        self.assertEqual(raised.exception.code, "STATIC_COMPLETION_CONTRADICTION")

    def test_static_release_flag_cannot_unblock_live_open_state(self) -> None:
        snapshot = _snapshot(issue_overrides={92: {"state": "open", "stateReason": None, "closedAt": None}})
        recovery = json.loads((release_gate.AUDIT_RECOVERY_PATH).read_text(encoding="utf-8"))
        recovery["releaseBlocked"] = False
        original_load = release_gate.load_json

        def load_with_mutated_recovery(path: Path) -> object:
            if path == release_gate.AUDIT_RECOVERY_PATH:
                return recovery
            return original_load(path)

        with mock.patch.object(release_gate, "current_head", return_value=SOURCE_SHA), mock.patch.object(state, "_now", return_value=NOW), mock.patch.object(release_gate, "load_json", side_effect=load_with_mutated_recovery):
            with self.assertRaises(release_gate.GateError) as raised:
                release_gate.check_audit_recovery_release_boundary(issue_state=snapshot)
        self.assertEqual(raised.exception.code, "STATIC_RELEASE_STATE_CONTRADICTION")

    def test_stale_direct_index_name_is_not_accepted_for_independent_contract(self) -> None:
        original_load = release_gate.load_json
        query_contract = original_load(release_gate.QUERY_CONTRACT_PATH)
        query_contract["index"]["schema"] = release_gate.DIRECT_QUERY_INDEX_SCHEMA

        def load_with_stale_query_contract(path: Path) -> object:
            if path == release_gate.QUERY_CONTRACT_PATH:
                return query_contract
            return original_load(path)

        with mock.patch.object(release_gate, "load_json", side_effect=load_with_stale_query_contract):
            with self.assertRaises(release_gate.GateError) as raised:
                release_gate.check_phase2_contracts()
        self.assertEqual(raised.exception.code, "QUERY_CONTRACT_INDEX_SCHEMA")

    def test_smoke_release_gate_is_blocked_and_not_release_ready(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = release_gate.main([])
        self.assertEqual(exit_code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["mode"], "smoke")
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["releaseReady"])
        self.assertEqual(report["diagnostics"][0]["code"], "RELEASE_AUTHORITY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
