"""Check that a release tag names the version in pyproject.toml.

`v1.0` matches `1.0.0`: missing trailing components count as zero.
"""

import re
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
TAG = re.compile(r"v(\d+(?:\.\d+)*)")


def _parts(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def tag_matches(tag: str, version: str) -> bool:
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError(
            f"malformed tag: {tag!r} (expected v<major>[.<minor>[.<patch>]])"
        )
    tagged, released = _parts(match.group(1)), _parts(version)
    width = max(len(tagged), len(released))
    return tagged + (0,) * (width - len(tagged)) == released + (0,) * (
        width - len(released)
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_version.py <tag>", file=sys.stderr)
        return 2
    version = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    try:
        matches = tag_matches(argv[1], version)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    if not matches:
        print(
            f"tag {argv[1]} does not match pyproject version {version}", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
