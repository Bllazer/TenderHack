"""Run supplied review scripts unchanged apart from local paths; keep results outside git."""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = root / ".tools/review-e382611"
    (output / "analysis").mkdir(parents=True, exist_ok=True)
    blocks = re.findall(r"### (independent_\w+\.py)\s+```python\s*\n(.*?)\n```", args.report.read_text(encoding="utf-8"), re.S)
    names = {"independent_tenderhack_audit.py", "independent_real_algorithm_audit.py",
             "independent_api_audit.py", "independent_api_numeric_grid.py"}
    for name, source in blocks:
        if name not in names:
            continue  # Search rebuild scripts generate new artifacts, outside this backend fix.
        source = source.replace("/workspace/scratch/708b3f7d3ba6/audit-e382611/backend", root.as_posix())
        source = source.replace("/workspace/scratch/708b3f7d3ba6", output.as_posix())
        source = source.replace("BASE/'audit-e382611/backend'", "Path(" + repr(root.as_posix()) + ")")
        source = source.replace("B/'audit-e382611/backend'", "Path(" + repr(root.as_posix()) + ")")
        script = output / name
        script.write_text(source, encoding="utf-8")
        result = subprocess.run([sys.executable, str(script)], cwd=root,
                                env={**os.environ, "PYTHONUTF8":"1", "PYTHONIOENCODING":"utf-8"})
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
