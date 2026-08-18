
#!/usr/bin/env python3
"""Generate requirement-to-test traceability matrices."""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def outputs(root: Path) -> dict[str, str]:
    requirements = load(root / "machine/requirements.yaml")["requirements"]
    tests = load(root / "machine/acceptance-tests.yaml")["tests"]
    by_requirement: dict[str, list[str]] = {item["id"]: [] for item in requirements}
    for test in tests:
        for requirement in test["requirements"]:
            by_requirement.setdefault(requirement, []).append(test["id"])

    md = ["# Requirement / acceptance-test traceability", "", "> Generated; do not edit manually.", "", "| Requirement | Level | Acceptance tests | Text |", "|---|---|---|---|"]
    for item in requirements:
        test_ids = ", ".join(f"`{test}`" for test in sorted(by_requirement[item["id"]]))
        md.append(f"| `{item['id']}` | {item['level']} | {test_ids} | {item['text']} |")
    md_text = "\n".join(md) + "\n"

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["requirement_id", "level", "acceptance_tests", "text"])
    for item in requirements:
        writer.writerow([item["id"], item["level"], ";".join(sorted(by_requirement[item["id"]])), item["text"]])
    return {"matrices/requirements-tests.md": md_text, "matrices/requirements-tests.csv": buffer.getvalue()}


def run(root: Path, check: bool) -> int:
    failures = []
    for relative, content in outputs(root).items():
        path = root / relative
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                failures.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if failures:
        print("traceability mismatch:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("traceability: ok" if check else "traceability: written")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    return run(Path(args.root).resolve(), args.check)


if __name__ == "__main__":
    raise SystemExit(main())
