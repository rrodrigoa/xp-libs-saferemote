"""Require passing main-branch checks on the exact release commit."""

import json
import os
import re
import subprocess

REQUIRED = {
    "release-safety",
    "dependency-audit",
    "codeql",
    "docs-skill",
    "compatibility-gate",
}


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo
    ):
        raise SystemExit("Invalid release repository or commit")
    raw = subprocess.check_output(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/commits/{commit}/check-runs?per_page=100",
        ],
        text=True,
        encoding="utf-8",
    )
    checks = {}
    for page in json.loads(raw):
        for run in page["check_runs"]:
            if run["app"]["slug"] != "github-actions":
                continue
            name = run["name"]
            if name not in checks or checks[name]["id"] < run["id"]:
                checks[name] = run
    missing = {
        name for name in REQUIRED if checks.get(name, {}).get("conclusion") != "success"
    }
    if missing:
        raise SystemExit(
            f"Required checks failed or missing: {', '.join(sorted(missing))}"
        )


if __name__ == "__main__":
    main()
