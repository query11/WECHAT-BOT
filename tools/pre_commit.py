"""Classify staged paths without Git's display quoting or shell decoding.

Exit 0 requests checks, 1 skips a docs-only commit, and 2 reports a Git error.
"""
import subprocess
import sys


def needs_checks():
    raw = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "--no-renames", "-z"],
        stderr=subprocess.PIPE)
    paths = [name for name in raw.split(b"\0") if name]
    return not paths or any(name.lower().endswith((b".py", b".json", b".ps1", b".cs"))
                            for name in paths)


def main():
    try:
        return 0 if needs_checks() else 1
    except (OSError, subprocess.CalledProcessError):
        print("[pre-commit] cannot read staged paths; commit blocked.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
