"""Check independent line/branch coverage, globally and per critical file."""

import json
import sys
from pathlib import Path


def verify(report: dict, critical: list[str]) -> list[str]:
    files = {name.replace("\\", "/"): row for name, row in report["files"].items()}
    failed = []
    if not report["meta"]["branch_coverage"]:
        return ["branch coverage was not measured"]
    targets = [("global", report["totals"], 90)]
    for path in critical:
        if path not in files:
            failed.append(f"{path} is missing from coverage")
        else:
            targets.append((path, files[path]["summary"], 95))
    for name, row, minimum in targets:
        statements = row["num_statements"]
        branches = row["num_branches"]
        line = 100 * row["covered_lines"] / statements if statements else 100
        branch = 100 * row["covered_branches"] / branches if branches else 100
        print(f"{name} line={line:.2f}% branch={branch:.2f}%")
        if line < minimum or branch < 90:
            failed.append(f"{name} requires {minimum}% line and 90% branch")
    return failed


if __name__ == "__main__":
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    critical = json.loads(
        Path("config/critical_coverage.json").read_text(encoding="utf-8")
    )
    failures = verify(report, critical)
    if failures:
        raise SystemExit("\n".join(failures))
