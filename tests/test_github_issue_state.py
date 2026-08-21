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
EXPECTED_ISSUE_NUMBERS = tuple(range(87, 106)) + tuple(range(108, 114))


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
            "apiUrl": f"https://api.github.com/repos/{state.REPOSITORY}/issues/{number}",
            "url": f"https://github.com/{state.REPOSITORY}/issues/{number}",
        }
        item.update(overrides.get(number, {}))
        item["etag"] = None
        item["responseDigest"] = state.sha256_json(state._response_record(item))
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
    def test_live_snapshot_records_bound_api_metadata(self) -> None:
        requested: list[int] = []

        def live(request: object, **_: object) -> _Response:
            url = request.full_url  # type: ignore[attr-defined]
            number = int(url.rsplit("/", 1)[-1])
            requested.append(number)
            timestamp = state.format_datetime(NOW - timedelta(minutes=5))
            return _Response(
                {
                    "number": number,
                    "state": "closed",
                    "state_reason": "completed",
                    "closed_at": timestamp,
                    "updated_at": timestamp,
                    "url": url,
                    "html_url": f"https://github.com/{state.REPOSITORY}/issues/{number}",
                    "repository_url": f"https://api.github.com/repos/{state.REPOSITORY}",
                }
            )

        snapshot = state.fetch_live_issue_state(
            source_sha=SOURCE_SHA,
            now=NOW,
            environment={"GITHUB_ACTOR": "test"},
            opener=live,
        )
        issue = snapshot["issues"][0]
        self.assertEqual(state.AUDIT_ISSUE_NUMBERS, EXPECTED_ISSUE_NUMBERS)
        self.assertEqual(tuple(requested), EXPECTED_ISSUE_NUMBERS)
        self.assertEqual(tuple(item["issueNumber"] for item in snapshot["issues"]), EXPECTED_ISSUE_NUMBERS)
        self.assertNotIn(106, requested)
        self.assertNotIn(107, requested)
        self.assertEqual(issue["issueNumber"], 87)
        self.assertEqual(issue["apiUrl"], "https://api.github.com/repos/horiyamayoh/fdir/issues/87")
        self.assertEqual(issue["etag"], '"test-etag"')
        self.assertEqual(issue["responseDigest"], state.sha256_json(state._response_record(issue)))
        self.assertEqual(snapshot["responseDigest"], state.sha256_json(state._response_records(snapshot)))
        self.assertEqual(snapshot["snapshotDigest"], state.sha256_json(state._snapshot_without_digest(snapshot)))
        self.assertEqual(snapshot["sourceSha"], SOURCE_SHA)
        state.validate_snapshot(snapshot, expected_source_sha=SOURCE_SHA, now=NOW)

    def test_live_target_set_rejects_pull_request_numbers(self) -> None:
        for pull_request_number in (106, 107):
            with self.subTest(pull_request_number=pull_request_number):
                with self.assertRaises(state.IssueStateError) as raised:
                    state.fetch_live_issue_state(
                        source_sha=SOURCE_SHA,
                        issue_numbers=(*EXPECTED_ISSUE_NUMBERS[:-1], pull_request_number),
                        now=NOW,
                    )
                self.assertEqual(raised.exception.code, "ISSUE_SNAPSHOT_SCOPE")

    def test_schema_declares_the_exact_required_issue_set(self) -> None:
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas" / "github-issue-state.schema.json").read_text(
                encoding="utf-8"
            )
        )
        issues_schema = schema["properties"]["issues"]
        issue_schema = schema["$defs"]["issue"]
        self.assertEqual(issues_schema["minItems"], len(EXPECTED_ISSUE_NUMBERS))
        self.assertEqual(issues_schema["maxItems"], len(EXPECTED_ISSUE_NUMBERS))
        self.assertEqual(
            [item["allOf"][1]["properties"]["issueNumber"]["const"] for item in issues_schema["prefixItems"]],
            list(EXPECTED_ISSUE_NUMBERS),
        )
        self.assertEqual(issue_schema["properties"]["issueNumber"]["enum"], list(EXPECTED_ISSUE_NUMBERS))
        self.assertNotIn(106, issue_schema["properties"]["issueNumber"]["enum"])
        self.assertNotIn(107, issue_schema["properties"]["issueNumber"]["enum"])

    def test_api_unavailable_fails_closed(self) -> None:
        def unavailable(*_: object, **__: object) -> object:
            raise urllib.error.URLError("offline")

        with self.assertRaises(state.IssueStateError) as raised:
            state.fetch_live_issue_state(source_sha=SOURCE_SHA, now=NOW, opener=unavailable)
        self.assertEqual(raised.exception.code, "GITHUB_API_UNAVAILABLE")

    def test_http_api_failure_fails_closed(self) -> None:
        def unavailable(request: object, **__: object) -> object:
            url = request.full_url  # type: ignore[attr-defined]
            raise urllib.error.HTTPError(url, 500, "server error", {}, io.BytesIO())

        with self.assertRaises(state.IssueStateError) as raised:
            state.fetch_live_issue_state(source_sha=SOURCE_SHA, now=NOW, opener=unavailable)
        self.assertEqual(raised.exception.code, "GITHUB_API_UNAVAILABLE")

    def test_open_and_reopened_issue_block_the_boundary(self) -> None:
        snapshot = _snapshot(issue_overrides={90: {"state": "open", "stateReason": "reopened", "closedAt": None}})
        boundary = state.derive_release_boundary(snapshot, now=NOW)
        self.assertTrue(boundary["releaseBlocked"])
        self.assertIn("GITHUB_ISSUE_REOPENED", {item["code"] for item in boundary["blockingIssues"]})

    def test_plain_open_issue_blocks_the_boundary(self) -> None:
        snapshot = _snapshot(issue_overrides={90: {"state": "open", "stateReason": None, "closedAt": None}})
        boundary = state.derive_release_boundary(snapshot, now=NOW)
        self.assertEqual(boundary["status"], "release-blocked")
        self.assertEqual(boundary["openIssues"], [90])
        self.assertIn("GITHUB_ISSUE_OPEN", {item["code"] for item in boundary["blockingIssues"]})

    def test_stale_snapshot_is_rejected(self) -> None:
        snapshot = _snapshot()
        with self.assertRaises(state.IssueStateError) as raised:
            state.validate_snapshot(snapshot, expected_source_sha=SOURCE_SHA, now=NOW + timedelta(seconds=state.DEFAULT_MAX_AGE_SECONDS + 1))
        self.assertEqual(raised.exception.code, "ISSUE_SNAPSHOT_STALE")

    def test_expired_snapshot_is_rejected_at_expiry_boundary(self) -> None:
        snapshot = _snapshot()
        with self.assertRaises(state.IssueStateError) as raised:
            state.validate_snapshot(
                snapshot,
                expected_source_sha=SOURCE_SHA,
                now=NOW + timedelta(seconds=state.DEFAULT_MAX_AGE_SECONDS),
            )
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

    def test_tampered_issue_state_is_rejected_by_response_digest(self) -> None:
        snapshot = _snapshot()
        snapshot["issues"][0]["stateReason"] = "not_planned"
        with self.assertRaises(state.IssueStateError) as raised:
            state.validate_snapshot(snapshot, expected_source_sha=SOURCE_SHA, now=NOW)
        self.assertEqual(raised.exception.code, "ISSUE_RESPONSE_DIGEST_MISMATCH")

    def test_tampered_snapshot_response_digest_is_rejected(self) -> None:
        snapshot = _snapshot()
        snapshot["responseDigest"] = "0" * 64
        with self.assertRaises(state.IssueStateError) as raised:
            state.validate_snapshot(snapshot, expected_source_sha=SOURCE_SHA, now=NOW)
        self.assertEqual(raised.exception.code, "ISSUE_RESPONSE_DIGEST_MISMATCH")

    def test_tampered_snapshot_digest_is_rejected(self) -> None:
        snapshot = _snapshot()
        snapshot["retrievedBy"] = "tampered"
        with self.assertRaises(state.IssueStateError) as raised:
            state.validate_snapshot(snapshot, expected_source_sha=SOURCE_SHA, now=NOW)
        self.assertEqual(raised.exception.code, "ISSUE_SNAPSHOT_DIGEST_MISMATCH")

    def test_issue_api_url_is_bound_to_repository_and_number(self) -> None:
        snapshot = _snapshot()
        snapshot["issues"][0]["apiUrl"] = "https://api.github.com/repos/someone/else/issues/87"
        with self.assertRaises(state.IssueStateError) as raised:
            state.validate_snapshot(snapshot, expected_source_sha=SOURCE_SHA, now=NOW)
        self.assertEqual(raised.exception.code, "GITHUB_API_URL_MISMATCH")

    def test_unknown_status_claim_is_rejected_even_when_digest_is_recomputed(self) -> None:
        snapshot = _snapshot()
        snapshot["status"] = "release-ready"
        snapshot["snapshotDigest"] = state.sha256_json(state._snapshot_without_digest(snapshot))
        with self.assertRaises(state.IssueStateError) as raised:
            state.validate_snapshot(snapshot, expected_source_sha=SOURCE_SHA, now=NOW)
        self.assertEqual(raised.exception.code, "ISSUE_SNAPSHOT_FIELDS")

    def test_status_only_claim_is_rejected_without_provenance(self) -> None:
        with self.assertRaises(state.IssueStateError) as raised:
            state.validate_snapshot(
                {
                    "repository": state.REPOSITORY,
                    "sourceSha": SOURCE_SHA,
                    "status": "completed",
                    "issues": [{"issueNumber": 87, "state": "closed"}],
                },
                expected_source_sha=SOURCE_SHA,
                now=NOW,
            )
        self.assertIn(raised.exception.code, {"ISSUE_SNAPSHOT_FIELDS", "ISSUE_SNAPSHOT_SCHEMA"})

    def test_boundary_rejects_stale_snapshot_instead_of_reanchoring_to_retrieval_time(self) -> None:
        snapshot = _snapshot()
        with self.assertRaises(state.IssueStateError) as raised:
            state.derive_release_boundary(snapshot, now=NOW + timedelta(seconds=state.DEFAULT_MAX_AGE_SECONDS))
        self.assertEqual(raised.exception.code, "ISSUE_SNAPSHOT_STALE")

    def test_static_completion_contradiction_cannot_override_open_live_state(self) -> None:
        snapshot = _snapshot(issue_overrides={91: {"state": "open", "stateReason": None, "closedAt": None}})
        with mock.patch.object(release_gate, "AUDIT_RECOVERY_ISSUES", state.AUDIT_ISSUE_NUMBERS), mock.patch.object(release_gate, "current_head", return_value=SOURCE_SHA), mock.patch.object(state, "_now", return_value=NOW):
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

        with mock.patch.object(release_gate, "AUDIT_RECOVERY_ISSUES", state.AUDIT_ISSUE_NUMBERS), mock.patch.object(release_gate, "current_head", return_value=SOURCE_SHA), mock.patch.object(state, "_now", return_value=NOW), mock.patch.object(release_gate, "load_json", side_effect=load_with_mutated_recovery):
            with self.assertRaises(release_gate.GateError) as raised:
                release_gate.check_audit_recovery_release_boundary(issue_state=snapshot)
        self.assertEqual(raised.exception.code, "STATIC_RELEASE_STATE_CONTRADICTION")

    def test_release_gate_rejects_local_provider_contract(self) -> None:
        contract = json.loads((release_gate.QUALIFICATION_CONTRACT_PATH).read_text(encoding="utf-8"))
        self.assertEqual(contract["ciPolicy"]["allowedProviders"], ["github-actions"])
        contract["ciPolicy"]["allowedProviders"] = ["local"]

        with self.assertRaises(release_gate.GateError) as raised:
            release_gate.check_recovery_scope_contract(contract)
        self.assertEqual(raised.exception.code, "CI_PROVIDER_REQUIRED")

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

    def test_explicit_release_mode_without_authority_fails_closed(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = release_gate.main(["--mode", "release"])
        self.assertEqual(exit_code, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["mode"], "release")
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["releaseReady"])
        self.assertEqual(report["diagnostics"][0]["code"], "RELEASE_AUTHORITY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
