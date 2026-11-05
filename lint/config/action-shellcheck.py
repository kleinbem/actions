"""Shellcheck the bash `run:` steps of composite actions; actionlint only reads workflows."""
import re
import subprocess
import sys
import tempfile

import yaml

EXPR = re.compile(r"\$\{\{.*?\}\}", re.S)
rc = 0
for path in sys.argv[1:]:
    doc = yaml.safe_load(open(path)) or {}
    for i, step in enumerate((doc.get("runs") or {}).get("steps") or []):
        shell, run = step.get("shell", ""), step.get("run")
        if not run or not re.match(r"(ba)?sh\b", shell):
            continue
        # Same substitution actionlint uses: expressions become a plain word.
        body = EXPR.sub("_expr_", run)
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
            f.write("#!/usr/bin/env bash\n" + body)
            f.flush()
            p = subprocess.run(["shellcheck", "-f", "gcc", f.name], capture_output=True, text=True)
        if p.returncode:
            rc = 1
            name = step.get("name") or step.get("id") or f"step {i}"
            for line in p.stdout.splitlines():
                # gcc format: file:line:col: msg; line 1 is our shebang.
                _, ln, rest = line.split(":", 2)
                print(f"{path} [{name}] run line {int(ln) - 1}:{rest}")
sys.exit(rc)
