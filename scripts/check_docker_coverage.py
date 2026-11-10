"""Check combined core and example-pack coverage independently."""

import json
import sys
from pathlib import Path

from check_critical_coverage import verify


def totals(files):
    fields = ("num_statements", "num_branches", "covered_lines", "covered_branches")
    return {
        field: sum(file["summary"][field] for file in files.values())
        for field in fields
    }


report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
critical = json.loads(Path("config/critical_coverage.json").read_text(encoding="utf-8"))
files = {name.replace("\\", "/"): row for name, row in report["files"].items()}
core = {
    name: row for name, row in files.items() if name.startswith("src/remote_intent/")
}
pack = {
    name: row
    for name, row in files.items()
    if name.startswith("examples/action_packs/evidence_export/")
}
if not core or "examples/action_packs/evidence_export/actions.py" not in pack:
    raise SystemExit("Combined coverage is missing core or example pack data")
errors = []
for label, entries, modules in (("SDK", core, critical), ("example pack", pack, [])):
    print(label)
    errors.extend(
        verify({**report, "files": entries, "totals": totals(entries)}, modules)
    )
if errors:
    raise SystemExit("\n".join(errors))
