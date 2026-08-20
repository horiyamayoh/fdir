from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools import adapter_protocol

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPOSITORY_ROOT / "fixtures/adapter-protocol"
MANIFEST_DIGEST = (
    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)
EXECUTABLE_DIGEST = (
    "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
)


class AdapterProtocolTests(unittest.TestCase):
    def test_rust_and_python_share_strict_wire_vectors(self) -> None:
        valid = adapter_protocol.load_json(FIXTURES / "valid-output.json")
        decoded = adapter_protocol.validate_envelope(valid)
        self.assertEqual(decoded["body"]["lane"], "native-substrate-census")

        cases = {
            "invalid-version.json": "FDIR-PROTOCOL-VERSION-MISMATCH",
            "unknown-critical-field.json": (
                "FDIR-PROTOCOL-UNKNOWN-CRITICAL-FIELD"
            ),
            "lane-substitution.json": "FDIR-PROTOCOL-SEMANTIC-OUTPUT-FIELD",
            "path-artifact.json": "FDIR-PROTOCOL-ARTIFACT-HANDLE",
        }
        for name, code in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(adapter_protocol.ProtocolViolation) as raised:
                    adapter_protocol.validate_envelope(
                        adapter_protocol.load_json(FIXTURES / name)
                    )
                self.assertEqual(raised.exception.code, code)

    def test_execute_envelope_has_complete_identity_and_resource_contract(self) -> None:
        execute = adapter_protocol.load_json(FIXTURES / "valid-execute.json")
        decoded = adapter_protocol.validate_envelope(execute)
        self.assertEqual(decoded["kind"], "execute")
        self.assertEqual(
            decoded["body"]["artifact"]["handle"], "artifact:source-1"
        )
        self.assertEqual(decoded["body"]["budget"]["maxInFlightChunks"], 2)

        unknown = copy.deepcopy(execute)
        unknown["body"]["futureBudgetEscape"] = True
        with self.assertRaises(adapter_protocol.ProtocolViolation) as raised:
            adapter_protocol.validate_envelope(unknown)
        self.assertEqual(raised.exception.code, "FDIR-PROTOCOL-EXECUTE-FIELD")

        invalid_budget = copy.deepcopy(execute)
        invalid_budget["body"]["budget"]["maxChunkBytes"] = 2048
        with self.assertRaises(adapter_protocol.ProtocolViolation) as raised:
            adapter_protocol.validate_envelope(invalid_budget)
        self.assertEqual(raised.exception.code, "FDIR-PROTOCOL-BUDGET-CHUNK")

    def test_worker_manifest_enforces_exact_facts_lanes_and_isolation(self) -> None:
        manifest = adapter_protocol.load_json(FIXTURES / "worker-manifest.json")
        adapter_protocol.validate_worker_manifest(manifest)

        floating = copy.deepcopy(manifest)
        floating["dependencies"][0]["version"] = "3.12.*"
        with self.assertRaises(adapter_protocol.ProtocolViolation) as raised:
            adapter_protocol.validate_worker_manifest(floating)
        self.assertEqual(
            raised.exception.code, "FDIR-PROTOCOL-DEPENDENCY-VERSION"
        )

        in_process = copy.deepcopy(manifest)
        in_process["processBoundary"] = "in-process"
        with self.assertRaises(adapter_protocol.ProtocolViolation) as raised:
            adapter_protocol.validate_worker_manifest(in_process)
        self.assertEqual(raised.exception.code, "FDIR-PROTOCOL-WORKER-ISOLATION")

        lane_escape = copy.deepcopy(manifest)
        lane_escape["capabilities"][0]["lanes"] = ["semantic-helper"]
        with self.assertRaises(adapter_protocol.ProtocolViolation) as raised:
            adapter_protocol.validate_worker_manifest(lane_escape)
        self.assertEqual(raised.exception.code, "FDIR-PROTOCOL-CAPABILITY-SCOPE")

        false_claim = copy.deepcopy(manifest)
        false_claim["capabilities"][0]["qualificationState"] = (
            "production-qualified"
        )
        with self.assertRaises(adapter_protocol.ProtocolViolation) as raised:
            adapter_protocol.validate_worker_manifest(false_claim)
        self.assertEqual(
            raised.exception.code, "FDIR-PROTOCOL-CAPABILITY-QUALIFICATION"
        )

    def test_sandbox_receipt_requires_every_fail_closed_control(self) -> None:
        receipt = adapter_protocol.load_json(FIXTURES / "sandbox-receipt.json")
        adapter_protocol.validate_sandbox_receipt(
            receipt,
            worker_id="mock-python-worker",
            manifest_digest=MANIFEST_DIGEST,
            executable_digest=EXECUTABLE_DIGEST,
        )
        controls = [
            "networkDenied",
            "opaqueHandlesOnly",
            "isolatedTemporaryStorage",
            "environmentCleared",
            "credentialsCleared",
            "childProcessesDenied",
            "inputReadOnly",
            "resourceLimitsEnforced",
        ]
        for control in controls:
            with self.subTest(control=control):
                denied = copy.deepcopy(receipt)
                denied[control] = False
                with self.assertRaises(adapter_protocol.ProtocolViolation) as raised:
                    adapter_protocol.validate_sandbox_receipt(
                        denied,
                        worker_id="mock-python-worker",
                        manifest_digest=MANIFEST_DIGEST,
                        executable_digest=EXECUTABLE_DIGEST,
                    )
                self.assertEqual(raised.exception.code, "FDIR-SANDBOX-DENIED")

    def test_replay_identity_is_deterministic_and_length_delimited(self) -> None:
        values = [
            "request-1",
            "artifact:source-1",
            MANIFEST_DIGEST,
            "1.0.0",
            "test-native-census",
            "conformance",
            "native-substrate-census",
        ]
        first = adapter_protocol.replay_key(values)
        second = adapter_protocol.replay_key(values)
        self.assertEqual(first, second)
        self.assertNotEqual(first, adapter_protocol.replay_key(values[:-1] + ["storage-codec"]))
        self.assertEqual(
            hashlib.sha256(first.encode("utf-8")).hexdigest(),
            hashlib.sha256(second.encode("utf-8")).hexdigest(),
        )

    def test_mock_non_rust_worker_passes_deterministic_complete_path(self) -> None:
        first = adapter_protocol.run_mock_worker("complete")
        second = adapter_protocol.run_mock_worker("complete")
        self.assertEqual(first.outcome, "complete")
        self.assertEqual(first.diagnostic_code, "FDIR-WORKER-COMPLETE")
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(len(first.envelopes), 2)
        self.assertEqual(first.envelopes[0]["kind"], "output")
        self.assertEqual(first.envelopes[1]["kind"], "terminal")

    def test_mock_worker_failures_remain_explicit_and_distinct(self) -> None:
        cases = {
            "identity-mismatch": "identity-mismatch",
            "lane-mismatch": "identity-mismatch",
            "protocol-mismatch": "protocol-mismatch",
            "malformed": "malformed-response",
            "truncated": "truncated-output",
            "crash": "worker-crash",
            "resource": "resource-limited",
            "sandbox-denied": "sandbox-denied",
        }
        observed: set[str] = set()
        for scenario, expected in cases.items():
            with self.subTest(scenario=scenario):
                outcome = adapter_protocol.run_mock_worker(
                    scenario, max_output_bytes=4096
                )
                self.assertEqual(outcome.outcome, expected)
                self.assertNotEqual(outcome.outcome, "complete")
                observed.add(outcome.outcome)
        self.assertGreaterEqual(len(observed), 7)

    def test_timeout_and_cancellation_are_not_crash_or_success(self) -> None:
        timed_out = adapter_protocol.run_mock_worker(
            "timeout", timeout_seconds=0.05
        )
        cancelled = adapter_protocol.run_mock_worker(
            "cancel", cancel_after_seconds=0.05
        )
        self.assertEqual(timed_out.outcome, "timed-out")
        self.assertEqual(cancelled.outcome, "cancelled")
        self.assertNotEqual(timed_out.outcome, cancelled.outcome)

    def test_conformance_process_receives_minimal_environment_and_isolated_cwd(self) -> None:
        probe = adapter_protocol.probe_mock_worker_environment()
        self.assertFalse(probe["homePresent"])
        self.assertEqual(probe["credentialKeys"], [])
        self.assertEqual(
            set(probe["environmentKeys"]),
            {"FDIR_PROTOCOL_VERSION", "LANG", "LC_ALL", "TZ"},
        )
        self.assertNotEqual(Path(probe["cwd"]), REPOSITORY_ROOT)
        self.assertIn("fdir-adapter-worker-", Path(probe["cwd"]).name)

    def test_duplicate_json_members_fail_before_protocol_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_text('{"schema":"a","schema":"b"}\n', encoding="utf-8")
            with self.assertRaises(adapter_protocol.ProtocolViolation) as raised:
                adapter_protocol.load_json(path)
        self.assertEqual(raised.exception.code, "FDIR-PROTOCOL-DUPLICATE-FIELD")

    def test_machine_contract_records_no_production_claim(self) -> None:
        policy = json.loads(
            (REPOSITORY_ROOT / "quality/adapter-protocol.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(policy["schema"], "fdir/adapter-protocol-quality/1")
        self.assertEqual(policy["protocolVersion"], adapter_protocol.PROTOCOL_VERSION)
        self.assertEqual(policy["capabilityState"], "implemented-unqualified")
        self.assertFalse(policy["productionReady"])
        self.assertEqual(policy["ownerIssue"], 12)
        self.assertIn("python-mock-worker", policy["conformanceImplementations"])
        self.assertIn("rust-sdk", policy["conformanceImplementations"])


if __name__ == "__main__":
    unittest.main()
