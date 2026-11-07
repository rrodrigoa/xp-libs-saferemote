"""Require 90% coverage on changed executable SDK lines."""

import argparse
import os
import re
import subprocess
from pathlib import Path

from defusedxml.ElementTree import parse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage_xml", type=Path)
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    base = os.environ.get("RIR_BASE_SHA")
    if not base or not re.fullmatch(r"[0-9a-f]{40}", base) or set(base) == {"0"}:
        raise SystemExit("RIR_BASE_SHA must name a reviewed comparison commit")
    revision = ["--cached", base] if args.staged else [base, "HEAD"]
    diff = subprocess.check_output(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--unified=0",
            *revision,
            "--",
            "src/remote_intent",
        ],
        text=True,
        encoding="utf-8",
    )
    coverage = {}
    for cls in parse(args.coverage_xml).iter("class"):
        filename = cls.attrib["filename"].replace("\\", "/")
        if not filename.startswith("src/remote_intent/"):
            filename = f"src/remote_intent/{filename}"
        coverage[filename] = {
            int(line.attrib["number"]): int(line.attrib["hits"])
            for line in cls.iter("line")
        }
    current = None
    covered = total = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line.removeprefix("+++ b/")
            if current.endswith(".py") and current not in coverage:
                raise SystemExit(f"No coverage measurement for {current}")
        if current in coverage and (
            match := re.match(r"@@ .* \+(\d+)(?:,(\d+))? @@", line)
        ):
            start = int(match[1])
            count = int(match[2] or 1)
            for number in range(start, start + count):
                if number in coverage[current]:
                    total += 1
                    covered += coverage[current][number] > 0
    print(f"Changed executable lines covered: {covered}/{total}")
    if total and covered * 100 < total * 90:
        raise SystemExit("Changed-line coverage is below 90%")


if __name__ == "__main__":
    main()
