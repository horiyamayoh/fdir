"""Acquire and verify the GitHub issue state used by the release barrier.

The committed recovery plan is a projection, not an authority.  This module
keeps the authority small and explicit: either the current GitHub REST
responses or a snapshot of those responses with enough metadata and digests to
be independently checked.  No caller may turn an unavailable API into a
successful or completed issue state.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable
import urllib.error
from urllib.parse import urlsplit
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_NAME = "fdir/github-issue-state"
VERSION = "1.0.0"
REPOSITORY = "horiyamayoh/fdir"
AUDIT_ISSUE_NUMBERS = tuple(range(87, 106)) + tuple(range(108, 114))
ISSUE_SCOPE_LABEL = "#87-#105 and #108-#113"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ALLOWED_STATE_REASONS = {None, "completed", "not_planned", "duplicate", "reopened"}
DEFAULT_MAX_AGE_SECONDS = 3600
DEFAULT_CLOCK_SKEW_SECONDS = 300
API_VERSION = "2022-11-28"
USER_AGENT = "fdir-release-gate/1.0"
SNAPSHOT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "repository",
        "sourceSha",
        "retrievedAt",
        "expiresAt",
        "maxAgeSeconds",
        "retrievedBy",
        "retrieval",
        "ci",
        "issues",
        "responseDigest",
        "snapshotDigest",
    }
)
RETRIEVAL_FIELDS = frozenset({"method", "apiBaseUrl", "apiVersion", "userAgent", "actor"})
CI_FIELDS = frozenset({"provider", "repository", "sourceSha", "runId", "attempt", "job"})
ISSUE_FIELDS = frozenset(
    {
        "issueNumber",
        "state",
        "stateReason",
        "closedAt",
        "updatedAt",
        "apiUrl",
        "url",
        "etag",
        "responseDigest",
    }
)


class IssueStateError(Exception):
    """A fail-closed issue-state error with a stable diagnostic code."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def format_datetime(value: datetime) -> str:
    return _now(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise IssueStateError("ISSUE_STATE_TIMESTAMP", f"{field} must be an RFC 3339 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise IssueStateError("ISSUE_STATE_TIMESTAMP", f"{field} is not an RFC 3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise IssueStateError("ISSUE_STATE_TIMESTAMP", f"{field} has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _repository(value: Any) -> str:
    if not isinstance(value, str) or REPOSITORY_RE.fullmatch(value) is None:
        raise IssueStateError("ISSUE_STATE_REPOSITORY", f"repository is invalid: {value!r}")
    return value


def _source_sha(value: Any, *, field: str = "sourceSha") -> str:
    if not isinstance(value, str) or SHA40.fullmatch(value) is None:
        raise IssueStateError("ISSUE_STATE_SHA", f"{field} must be a 40-character lowercase SHA")
    return value


def _positive_attempt(value: Any, *, field: str = "attempt") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise IssueStateError("ISSUE_STATE_ATTEMPT", f"{field} must be a positive integer")
    return value


def _reject_unknown_fields(value: dict[str, Any], allowed: frozenset[str], *, context: str) -> None:
    unknown = [key for key in value if key not in allowed]
    if unknown:
        raise IssueStateError(
            "ISSUE_SNAPSHOT_FIELDS",
            f"{context} contains unsupported field(s): {', '.join(repr(key) for key in unknown)}",
        )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise IssueStateError("ISSUE_SNAPSHOT_MISSING", f"snapshot does not exist: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IssueStateError("ISSUE_SNAPSHOT_INVALID", f"cannot read snapshot {path}: {exc}") from exc


def _issue_url(repository: str, issue_number: int) -> str:
    return f"https://github.com/{repository}/issues/{issue_number}"


def _api_base_url(value: Any, *, field: str = "apiBaseUrl") -> str:
    if not isinstance(value, str) or not value:
        raise IssueStateError("GITHUB_API_URL_INVALID", f"{field} must be an HTTPS URL")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise IssueStateError("GITHUB_API_URL_INVALID", f"{field} is not a valid HTTPS URL: {value!r}") from exc
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise IssueStateError("GITHUB_API_URL_INVALID", f"{field} is not a valid HTTPS URL: {value!r}")
    return value.rstrip("/")


def _api_issue_url(api_base_url: str, repository: str, issue_number: int) -> str:
    return f"{_api_base_url(api_base_url)}/repos/{repository}/issues/{issue_number}"


def _response_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in (
            "issueNumber",
            "state",
            "stateReason",
            "closedAt",
            "updatedAt",
            "apiUrl",
            "url",
            "etag",
        )
    }


def _response_projection(
    payload: Any,
    *,
    repository: str,
    issue_number: int,
    api_url: str,
    etag: str | None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise IssueStateError("GITHUB_API_RESPONSE_INVALID", f"issue #{issue_number} response is not an object")
    if payload.get("number") != issue_number:
        raise IssueStateError(
            "GITHUB_ISSUE_NUMBER_MISMATCH",
            f"GitHub returned issue {payload.get('number')!r} for requested issue #{issue_number}",
        )
    state = payload.get("state")
    if state not in {"open", "closed"}:
        raise IssueStateError("GITHUB_ISSUE_STATE_INVALID", f"issue #{issue_number} has invalid state {state!r}")
    state_reason = payload.get("state_reason")
    if state_reason not in ALLOWED_STATE_REASONS:
        raise IssueStateError("GITHUB_ISSUE_STATE_REASON_INVALID", f"issue #{issue_number} has invalid state reason {state_reason!r}")
    updated_at = payload.get("updated_at")
    parse_datetime(updated_at, field=f"issue #{issue_number}.updatedAt")
    closed_at = payload.get("closed_at")
    if closed_at is not None:
        parse_datetime(closed_at, field=f"issue #{issue_number}.closedAt")

    api_url = _api_base_url(api_url, field=f"issue #{issue_number}.apiUrl")
    expected_path_suffix = f"/repos/{repository}/issues/{issue_number}"
    if not urlsplit(api_url).path.rstrip("/").endswith(expected_path_suffix):
        raise IssueStateError("GITHUB_API_URL_MISMATCH", f"issue #{issue_number} API URL is not bound to the requested issue")
    response_api_url = payload.get("url")
    if response_api_url is not None and response_api_url != api_url:
        raise IssueStateError("GITHUB_API_URL_MISMATCH", f"issue #{issue_number} API URL is {response_api_url!r}, expected {api_url!r}")

    expected_html_url = _issue_url(repository, issue_number)
    html_url = payload.get("html_url")
    if html_url is not None and html_url != expected_html_url:
        raise IssueStateError("GITHUB_ISSUE_REPOSITORY_MISMATCH", f"issue #{issue_number} belongs to another repository: {html_url!r}")
    repository_url = payload.get("repository_url")
    expected_repository_url = api_url.rsplit("/issues/", 1)[0]
    if repository_url is not None and repository_url != expected_repository_url:
        raise IssueStateError("GITHUB_ISSUE_REPOSITORY_MISMATCH", f"issue #{issue_number} repository_url is {repository_url!r}")

    record = {
        "issueNumber": issue_number,
        "state": state,
        "stateReason": state_reason,
        "closedAt": closed_at,
        "updatedAt": updated_at,
        "apiUrl": api_url,
        "url": expected_html_url,
        "etag": etag if isinstance(etag, str) and etag else None,
    }
    return {**record, "responseDigest": sha256_json(_response_record(record))}


def _ci_metadata(repository: str, source_sha: str, environment: dict[str, str] | None = None) -> dict[str, Any]:
    env = environment if environment is not None else os.environ
    actions = env.get("GITHUB_ACTIONS", "").casefold() == "true"
    run_id = env.get("GITHUB_RUN_ID") if actions else "local"
    if actions and (not isinstance(run_id, str) or not re.fullmatch(r"[1-9][0-9]*", run_id)):
        raise IssueStateError("ISSUE_STATE_RUN_ID", "GitHub Actions GITHUB_RUN_ID is missing or invalid")
    attempt_value: Any = env.get("GITHUB_RUN_ATTEMPT", "1")
    if isinstance(attempt_value, str) and attempt_value.isdigit():
        attempt_value = int(attempt_value)
    attempt = _positive_attempt(attempt_value)
    job = env.get("GITHUB_JOB", "local")
    if not isinstance(job, str) or not job:
        raise IssueStateError("ISSUE_STATE_JOB", "issue-state retrieval job identity is missing")
    return {
        "provider": "github-actions" if actions else "local",
        "repository": repository,
        "sourceSha": source_sha,
        "runId": run_id or "local",
        "attempt": attempt,
        "job": job,
    }


def _snapshot_without_digest(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key != "snapshotDigest"}


def _response_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [_response_record(item) for item in snapshot.get("issues", []) if isinstance(item, dict)]


def _validate_common_snapshot_shape(
    snapshot: Any,
    *,
    expected_repository: str,
    expected_source_sha: str | None,
    expected_run_id: str | None,
    expected_attempt: int | None,
    now: datetime | None,
    max_age_seconds: int,
) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise IssueStateError("ISSUE_SNAPSHOT_ROOT", "issue-state snapshot root must be an object")
    _reject_unknown_fields(snapshot, SNAPSHOT_FIELDS, context="snapshot")
    if snapshot.get("schema") != SCHEMA_NAME or snapshot.get("version") != VERSION:
        raise IssueStateError("ISSUE_SNAPSHOT_SCHEMA", "issue-state snapshot schema/version is invalid")
    repository = _repository(snapshot.get("repository"))
    if repository != expected_repository:
        raise IssueStateError("ISSUE_STATE_REPOSITORY_MISMATCH", f"snapshot repository {repository!r} does not match {expected_repository!r}")
    source_sha = _source_sha(snapshot.get("sourceSha"))
    if expected_source_sha is not None and source_sha != _source_sha(expected_source_sha):
        raise IssueStateError("ISSUE_STATE_SHA_MISMATCH", f"snapshot source SHA {source_sha} does not match {expected_source_sha}")
    retrieved_at = parse_datetime(snapshot.get("retrievedAt"), field="retrievedAt")
    expires_at = parse_datetime(snapshot.get("expiresAt"), field="expiresAt")
    max_age = snapshot.get("maxAgeSeconds")
    maximum_age = min(max_age_seconds, DEFAULT_MAX_AGE_SECONDS)
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 1 or max_age > maximum_age:
        raise IssueStateError("ISSUE_SNAPSHOT_MAX_AGE", f"snapshot maxAgeSeconds must be between 1 and {maximum_age}")
    if expires_at != retrieved_at + timedelta(seconds=max_age):
        raise IssueStateError("ISSUE_SNAPSHOT_EXPIRY", "snapshot expiresAt does not match retrievedAt and maxAgeSeconds")
    if not isinstance(snapshot.get("retrievedBy"), str) or not snapshot["retrievedBy"]:
        raise IssueStateError("ISSUE_SNAPSHOT_PROVENANCE", "snapshot retrievedBy is missing")
    current = _now(now)
    if retrieved_at > current + timedelta(seconds=DEFAULT_CLOCK_SKEW_SECONDS):
        raise IssueStateError("ISSUE_SNAPSHOT_FUTURE", "snapshot retrievedAt is in the future")
    if current >= expires_at:
        raise IssueStateError("ISSUE_SNAPSHOT_STALE", f"snapshot expired at {snapshot.get('expiresAt')}")

    retrieval = snapshot.get("retrieval")
    if not isinstance(retrieval, dict):
        raise IssueStateError("ISSUE_SNAPSHOT_PROVENANCE", "snapshot retrieval metadata is missing")
    _reject_unknown_fields(retrieval, RETRIEVAL_FIELDS, context="snapshot.retrieval")
    for field in ("method", "apiBaseUrl", "apiVersion", "userAgent"):
        if not isinstance(retrieval.get(field), str) or not retrieval[field]:
            raise IssueStateError("ISSUE_SNAPSHOT_PROVENANCE", f"snapshot retrieval.{field} is missing")
    if retrieval.get("method") != "GET" or retrieval.get("apiVersion") != API_VERSION:
        raise IssueStateError("ISSUE_SNAPSHOT_PROVENANCE", "snapshot retrieval method or API version is invalid")
    api_base_url = _api_base_url(retrieval.get("apiBaseUrl"), field="snapshot.retrieval.apiBaseUrl")
    if "actor" in retrieval and (not isinstance(retrieval.get("actor"), str) or not retrieval["actor"]):
        raise IssueStateError("ISSUE_SNAPSHOT_PROVENANCE", "snapshot retrieval.actor is invalid")

    ci = snapshot.get("ci")
    if not isinstance(ci, dict):
        raise IssueStateError("ISSUE_STATE_CI_BINDING", "snapshot CI binding is missing")
    _reject_unknown_fields(ci, CI_FIELDS, context="snapshot.ci")
    if ci.get("provider") not in {"github-actions", "local"}:
        raise IssueStateError("ISSUE_STATE_CI_BINDING", "snapshot CI provider is invalid")
    if ci.get("repository") != repository or ci.get("sourceSha") != source_sha:
        raise IssueStateError("ISSUE_STATE_CI_BINDING", "snapshot CI repository or source SHA is inconsistent")
    attempt = _positive_attempt(ci.get("attempt"))
    run_id = ci.get("runId")
    if not isinstance(run_id, str) or not run_id:
        raise IssueStateError("ISSUE_STATE_RUN_ID", "snapshot CI runId is missing")
    if expected_run_id is not None and run_id != expected_run_id:
        raise IssueStateError("ISSUE_STATE_RUN_ID_MISMATCH", f"snapshot runId {run_id!r} does not match {expected_run_id!r}")
    if expected_attempt is not None and attempt != _positive_attempt(expected_attempt):
        raise IssueStateError("ISSUE_STATE_ATTEMPT_MISMATCH", f"snapshot attempt {attempt} does not match {expected_attempt}")

    issues = snapshot.get("issues")
    if not isinstance(issues, list) or len(issues) != len(AUDIT_ISSUE_NUMBERS):
        raise IssueStateError("ISSUE_SNAPSHOT_SCOPE", f"snapshot must contain exactly issues {ISSUE_SCOPE_LABEL}")
    if tuple(item.get("issueNumber") for item in issues if isinstance(item, dict)) != AUDIT_ISSUE_NUMBERS:
        raise IssueStateError("ISSUE_SNAPSHOT_SCOPE", f"snapshot issue order must be {ISSUE_SCOPE_LABEL}")
    by_number: dict[int, dict[str, Any]] = {}
    for item in issues:
        if not isinstance(item, dict):
            raise IssueStateError("ISSUE_SNAPSHOT_ENTRY", "snapshot issue entry is not an object")
        _reject_unknown_fields(item, ISSUE_FIELDS, context="snapshot issue entry")
        number = item.get("issueNumber")
        if isinstance(number, bool) or not isinstance(number, int) or number not in AUDIT_ISSUE_NUMBERS or number in by_number:
            raise IssueStateError("ISSUE_SNAPSHOT_SCOPE", f"snapshot issue number is invalid or duplicated: {number!r}")
        for field in ("state", "stateReason", "closedAt", "updatedAt", "apiUrl", "url", "etag", "responseDigest"):
            if field not in item:
                raise IssueStateError("ISSUE_SNAPSHOT_ENTRY", f"issue #{number} is missing {field}")
        if item.get("state") not in {"open", "closed"}:
            raise IssueStateError("GITHUB_ISSUE_STATE_INVALID", f"issue #{number} has invalid state")
        if item.get("stateReason") not in ALLOWED_STATE_REASONS:
            raise IssueStateError("GITHUB_ISSUE_STATE_REASON_INVALID", f"issue #{number} has invalid state reason")
        updated_at = parse_datetime(item.get("updatedAt"), field=f"issue #{number}.updatedAt")
        closed_at = parse_datetime(item.get("closedAt"), field=f"issue #{number}.closedAt") if item.get("closedAt") is not None else None
        if item.get("state") == "closed" and closed_at is None:
            raise IssueStateError("GITHUB_ISSUE_CLOSED_AT_MISSING", f"closed issue #{number} has no closedAt timestamp")
        if item.get("state") == "open" and closed_at is not None:
            raise IssueStateError("GITHUB_ISSUE_STATE_TIMESTAMP_MISMATCH", f"open issue #{number} has a closedAt timestamp")
        if closed_at is not None and closed_at > updated_at:
            raise IssueStateError("GITHUB_ISSUE_TIMESTAMP_ORDER", f"issue #{number} closedAt is later than updatedAt")
        if updated_at > retrieved_at + timedelta(seconds=DEFAULT_CLOCK_SKEW_SECONDS):
            raise IssueStateError("GITHUB_ISSUE_TIMESTAMP_FUTURE", f"issue #{number} updatedAt is after retrievedAt")
        if closed_at is not None and closed_at > retrieved_at + timedelta(seconds=DEFAULT_CLOCK_SKEW_SECONDS):
            raise IssueStateError("GITHUB_ISSUE_TIMESTAMP_FUTURE", f"issue #{number} closedAt is after retrievedAt")
        expected_api_url = _api_issue_url(api_base_url, repository, number)
        if item.get("apiUrl") != expected_api_url:
            raise IssueStateError("GITHUB_API_URL_MISMATCH", f"issue #{number} API URL does not match the retrieval endpoint")
        if item.get("url") != _issue_url(repository, number):
            raise IssueStateError("GITHUB_ISSUE_REPOSITORY_MISMATCH", f"issue #{number} URL belongs to another repository")
        etag = item.get("etag")
        if etag is not None and (not isinstance(etag, str) or not etag):
            raise IssueStateError("ISSUE_SNAPSHOT_ENTRY", f"issue #{number} ETag is invalid")
        expected_response_digest = sha256_json(_response_record(item))
        if item.get("responseDigest") != expected_response_digest:
            raise IssueStateError("ISSUE_RESPONSE_DIGEST_MISMATCH", f"issue #{number} response digest does not match its state")
        by_number[number] = item
    if set(by_number) != set(AUDIT_ISSUE_NUMBERS):
        raise IssueStateError("ISSUE_SNAPSHOT_SCOPE", f"snapshot issue scope does not cover {ISSUE_SCOPE_LABEL} exactly")
    if not isinstance(snapshot.get("responseDigest"), str) or SHA256.fullmatch(snapshot["responseDigest"]) is None:
        raise IssueStateError("ISSUE_RESPONSE_DIGEST_MISMATCH", "snapshot responseDigest is invalid")
    if snapshot.get("responseDigest") != sha256_json(_response_records(snapshot)):
        raise IssueStateError("ISSUE_RESPONSE_DIGEST_MISMATCH", "snapshot response digest does not match issue records")
    snapshot_digest = snapshot.get("snapshotDigest")
    if not isinstance(snapshot_digest, str) or SHA256.fullmatch(snapshot_digest) is None:
        raise IssueStateError("ISSUE_SNAPSHOT_DIGEST", "snapshotDigest is invalid")
    if snapshot_digest != sha256_json(_snapshot_without_digest(snapshot)):
        raise IssueStateError("ISSUE_SNAPSHOT_DIGEST_MISMATCH", "snapshotDigest does not match snapshot content")
    return {"repository": repository, "sourceSha": source_sha, "issuesByNumber": by_number, "retrievedAt": retrieved_at, "expiresAt": expires_at}


def validate_snapshot(
    snapshot: Any,
    *,
    expected_repository: str = REPOSITORY,
    expected_source_sha: str | None = None,
    expected_run_id: str | None = None,
    expected_attempt: int | None = None,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Validate a snapshot and return indexed metadata for release checks."""

    expected_repository = _repository(expected_repository)
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int)
        or max_age_seconds < 1
        or max_age_seconds > DEFAULT_MAX_AGE_SECONDS
    ):
        raise IssueStateError("ISSUE_SNAPSHOT_MAX_AGE", f"max_age_seconds must be between 1 and {DEFAULT_MAX_AGE_SECONDS}")
    shape = _validate_common_snapshot_shape(
        snapshot,
        expected_repository=expected_repository,
        expected_source_sha=expected_source_sha,
        expected_run_id=expected_run_id,
        expected_attempt=expected_attempt,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    return {**shape, "snapshot": snapshot, "authority": "snapshot"}


def _open_url(request: urllib.request.Request, timeout: int, opener: Callable[..., Any] | None) -> Any:
    return (opener or urllib.request.urlopen)(request, timeout=timeout)


def fetch_live_issue_state(
    *,
    repository: str = REPOSITORY,
    source_sha: str,
    issue_numbers: Iterable[int] = AUDIT_ISSUE_NUMBERS,
    api_base_url: str = "https://api.github.com",
    token: str | None = None,
    environment: dict[str, str] | None = None,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    timeout_seconds: int = 20,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Fetch all required issue responses and return a verifiable snapshot."""

    repository = _repository(repository)
    source_sha = _source_sha(source_sha)
    api_base_url = _api_base_url(api_base_url, field="GitHub API URL")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise IssueStateError("GITHUB_API_TIMEOUT", "GitHub API timeout must be positive")
    try:
        raw_numbers = tuple(issue_numbers)
        numbers = tuple(
            number
            for number in raw_numbers
            if not isinstance(number, bool) and isinstance(number, int)
        )
    except TypeError as exc:
        raise IssueStateError("ISSUE_SNAPSHOT_SCOPE", "live issue request must be an iterable of issue numbers") from exc
    if set(numbers) != set(AUDIT_ISSUE_NUMBERS) or len(numbers) != len(AUDIT_ISSUE_NUMBERS):
        raise IssueStateError("ISSUE_SNAPSHOT_SCOPE", f"live issue request must cover {ISSUE_SCOPE_LABEL} exactly")
    numbers = AUDIT_ISSUE_NUMBERS
    env = environment if environment is not None else os.environ
    token = token if token is not None else env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    retrieved_at = _now(now)
    states: list[dict[str, Any]] = []
    for issue_number in numbers:
        api_url = _api_issue_url(api_base_url, repository, issue_number)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(api_url, headers=headers, method="GET")
        try:
            with _open_url(request, timeout_seconds, opener) as response:
                status = getattr(response, "status", None)
                if status is None:
                    getcode = getattr(response, "getcode", None)
                    status = getcode() if callable(getcode) else None
                if status is not None and status != 200:
                    raise IssueStateError("GITHUB_API_UNAVAILABLE", f"GET {api_url} returned HTTP {status}")
                raw = response.read()
                response_headers = getattr(response, "headers", {})
        except urllib.error.HTTPError as exc:
            status = getattr(exc, "code", None)
            headers_value = getattr(exc, "headers", {})
            remaining = headers_value.get("X-RateLimit-Remaining") if headers_value is not None else None
            if status == 404:
                code = "GITHUB_API_NOT_FOUND"
            elif status == 401:
                code = "GITHUB_API_UNAUTHORIZED"
            elif status == 403 and str(remaining) == "0":
                code = "GITHUB_API_RATE_LIMITED"
            elif status == 403:
                code = "GITHUB_API_FORBIDDEN"
            elif status == 429:
                code = "GITHUB_API_RATE_LIMITED"
            else:
                code = "GITHUB_API_UNAVAILABLE"
            raise IssueStateError(code, f"GET {api_url} returned HTTP {status}") from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise IssueStateError("GITHUB_API_UNAVAILABLE", f"GET {api_url} failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(raw, bytes):
            raise IssueStateError("GITHUB_API_RESPONSE_INVALID", f"GET {api_url} did not return bytes")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IssueStateError("GITHUB_API_RESPONSE_INVALID", f"GET {api_url} did not return UTF-8 JSON") from exc
        etag = response_headers.get("ETag") or response_headers.get("etag") if response_headers is not None else None
        states.append(_response_projection(payload, repository=repository, issue_number=issue_number, api_url=api_url, etag=etag))

    max_age = max_age_seconds
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 1 or max_age > DEFAULT_MAX_AGE_SECONDS:
        raise IssueStateError("ISSUE_SNAPSHOT_MAX_AGE", f"max_age_seconds must be between 1 and {DEFAULT_MAX_AGE_SECONDS}")
    actor = env.get("GITHUB_ACTOR") or "github-api"
    if not isinstance(actor, str) or not actor:
        raise IssueStateError("ISSUE_STATE_PROVENANCE", "GitHub actor is missing")
    snapshot: dict[str, Any] = {
        "schema": SCHEMA_NAME,
        "version": VERSION,
        "repository": repository,
        "sourceSha": source_sha,
        "retrievedAt": format_datetime(retrieved_at),
        "expiresAt": format_datetime(retrieved_at + timedelta(seconds=max_age)),
        "maxAgeSeconds": max_age,
        "retrievedBy": actor,
        "retrieval": {
            "method": "GET",
            "apiBaseUrl": api_base_url,
            "apiVersion": API_VERSION,
            "userAgent": USER_AGENT,
            "actor": actor,
        },
        "ci": _ci_metadata(repository, source_sha, env),
        "issues": states,
    }
    snapshot["responseDigest"] = sha256_json(_response_records(snapshot))
    snapshot["snapshotDigest"] = sha256_json(snapshot)
    validate_snapshot(snapshot, expected_repository=repository, expected_source_sha=source_sha, now=retrieved_at, max_age_seconds=max_age)
    return snapshot


def load_snapshot(
    path: Path,
    *,
    expected_repository: str = REPOSITORY,
    expected_source_sha: str | None = None,
    expected_run_id: str | None = None,
    expected_attempt: int | None = None,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    snapshot = _load_json(path.resolve())
    validate_snapshot(
        snapshot,
        expected_repository=expected_repository,
        expected_source_sha=expected_source_sha,
        expected_run_id=expected_run_id,
        expected_attempt=expected_attempt,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    return snapshot


def resolve_issue_state(
    *,
    source_sha: str,
    snapshot_path: Path | None = None,
    repository: str = REPOSITORY,
    environment: dict[str, str] | None = None,
    now: datetime | None = None,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Resolve live API or an explicitly supplied snapshot, never a JSON plan."""

    env = environment if environment is not None else os.environ
    actions = env.get("GITHUB_ACTIONS", "").casefold() == "true"
    expected_run_id = env.get("GITHUB_RUN_ID") if actions else None
    attempt_value: Any = env.get("GITHUB_RUN_ATTEMPT") if actions else None
    if isinstance(attempt_value, str) and attempt_value.isdigit():
        attempt_value = int(attempt_value)
    if snapshot_path is not None:
        snapshot = load_snapshot(
            snapshot_path,
            expected_repository=repository,
            expected_source_sha=source_sha,
            expected_run_id=expected_run_id,
            expected_attempt=attempt_value,
            now=now,
        )
        return {"authority": "snapshot", "snapshot": snapshot}
    snapshot = fetch_live_issue_state(
        repository=repository,
        source_sha=source_sha,
        environment=env,
        now=now,
        opener=opener,
    )
    return {"authority": "live-api", "snapshot": snapshot}


def derive_release_boundary(snapshot: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Derive the release state from issue responses, not committed metadata."""

    source_sha = _source_sha(snapshot.get("sourceSha")) if isinstance(snapshot, dict) else None
    indexed = validate_snapshot(
        snapshot,
        expected_repository=REPOSITORY,
        expected_source_sha=source_sha,
        now=now,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
    )
    by_number = indexed["issuesByNumber"]
    blockers: list[dict[str, Any]] = []
    for issue_number in AUDIT_ISSUE_NUMBERS:
        issue = by_number[issue_number]
        state = issue.get("state")
        reason = issue.get("stateReason")
        if state == "open":
            blockers.append({"code": "GITHUB_ISSUE_REOPENED" if reason == "reopened" else "GITHUB_ISSUE_OPEN", "issueNumber": issue_number, "state": state, "stateReason": reason})
        elif reason != "completed":
            blockers.append({"code": "GITHUB_ISSUE_NOT_COMPLETED", "issueNumber": issue_number, "state": state, "stateReason": reason})
        elif issue.get("closedAt") is None:
            blockers.append({"code": "GITHUB_ISSUE_CLOSED_AT_MISSING", "issueNumber": issue_number, "state": state, "stateReason": reason})
    return {
        "status": "release-ready" if not blockers else "release-blocked",
        "releaseBlocked": bool(blockers),
        "blockingIssues": blockers,
        "openIssues": [item["issueNumber"] for item in blockers if item["state"] == "open"],
        "snapshotDigest": snapshot.get("snapshotDigest"),
        "retrievedAt": snapshot.get("retrievedAt"),
        "authority": "verified-snapshot",
    }


def evidence_close_time_blockers(
    snapshot: dict[str, Any],
    evidence_times: dict[int, datetime | str],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Reject evidence generated after the issue was already closed."""

    source_sha = _source_sha(snapshot.get("sourceSha")) if isinstance(snapshot, dict) else None
    indexed = validate_snapshot(
        snapshot,
        expected_repository=REPOSITORY,
        expected_source_sha=source_sha,
        now=now,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
    )
    blockers: list[dict[str, Any]] = []
    for issue_number, evidence_time in evidence_times.items():
        issue = indexed["issuesByNumber"].get(issue_number)
        if not issue or issue.get("closedAt") is None:
            continue
        close_time = parse_datetime(issue["closedAt"], field=f"issue #{issue_number}.closedAt")
        completed_at = parse_datetime(evidence_time, field=f"issue #{issue_number}.evidenceCompletedAt") if isinstance(evidence_time, str) else _now(evidence_time)
        if close_time < completed_at:
            blockers.append({"code": "GITHUB_ISSUE_CLOSED_BEFORE_EVIDENCE", "issueNumber": issue_number, "closedAt": issue["closedAt"], "evidenceCompletedAt": format_datetime(completed_at)})
    return blockers


def _emit(value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(rendered.encode("utf-8"))
    else:
        sys.stdout.write(rendered)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--snapshot", type=Path, help="validate and re-emit an existing snapshot instead of calling GitHub")
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.snapshot is not None:
            snapshot = load_snapshot(args.snapshot, expected_repository=args.repository, expected_source_sha=args.source_sha, max_age_seconds=args.max_age_seconds)
        else:
            snapshot = fetch_live_issue_state(repository=args.repository, source_sha=args.source_sha, max_age_seconds=args.max_age_seconds)
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        _emit({"schema": "fdir/github-issue-state-result", "version": VERSION, "status": "passed", "snapshot": snapshot, "boundary": derive_release_boundary(snapshot)})
        return 0
    except IssueStateError as exc:
        _emit({"schema": "fdir/github-issue-state-result", "version": VERSION, "status": "failed", "diagnostics": [{"code": exc.code, "detail": exc.detail}]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
