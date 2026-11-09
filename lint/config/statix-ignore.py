"""Drop the paths a repo's statix.toml ignores from stdin's list of Nix files.

statix applies `ignore` only while walking a directory, not to a file named
on the command line, and fleet-lint names tracked files one by one.
"""

import fnmatch
import sys
import tomllib
from pathlib import Path

ignore = []
config = Path("statix.toml")
if config.is_file():
    ignore = tomllib.loads(config.read_text()).get("ignore", [])

for line in sys.stdin:
    path = line.rstrip("\n")
    if path and not any(
        fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.rstrip("/") + "/*")
        for pattern in ignore
    ):
        print(path)
