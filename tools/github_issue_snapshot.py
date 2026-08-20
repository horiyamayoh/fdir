"""Fetch the live completion state required by the release evidence bundle.

The script is intentionally small and uses only the Python standard library so
the CI job can bind the issue state to the same commit as the qualification
reports.  It fails closed when credentials, API data, or the exact #87-#105
scope is unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
ISSUE_NUMBERS = list(range(87, 106))


class SnapshotError(RuntimeError):
    pass


def fetch_issue(repository: str, number: int, token: str | None) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/issues/{number}",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"GitHub issue #{number} could not be fetched: {exc}") from exc


def collect(repository: str, token: str | None) -> dict:
    issues = []
    for number in ISSUE_NUMBERS:
        issue = fetch_issue(repository, number, token)
        issues.append(
            {
                "number": number,
                "state": issue.get("state"),
                "stateReason": issue.get("state_reason"),
                "title": issue.get("title"),
                "url": issue.get("html_url"),
                "closedAt": issue.get("closed_at"),
            }
        )
    if any(item["state"] != "closed" or item["stateReason"] != "completed" for item in issues):
        bad = [item["number"] for item in issues if item["state"] != "closed" or item["stateReason"] != "completed"]
        raise SnapshotError(f"required GitHub issues are not closed as completed: {bad}")
    return {
        "schema": "fdir/github-issue-state-snapshot",
        "version": "1.0.0",
        "repository": repository,
        "source": "github-issue-api",
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch live #87-#105 GitHub completion state")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "horiyamayoh/fdir"))
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        value = collect(args.repository, token)
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "passed", "repository": args.repository, "issues": ISSUE_NUMBERS, "output": str(output)}, ensure_ascii=False))
        return 0
    except SnapshotError as exc:
        print(json.dumps({"schema": "fdir/github-issue-state-snapshot-error", "status": "failed", "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
