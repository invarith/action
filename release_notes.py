"""A release of this action: its version, which is the invarith release's it names,
and its notes, from `release/` (README.md, Releases; invarith/backend's decision
D56). The release workflow runs it on a push to `main`:

- `release_notes.py version` prints the version `release/invarith.json` names, or
  nothing if there's no `release/`, and fails if it isn't `X.Y.Z`;
- `release_notes.py notes` prints the release's notes: invarith's packages with their
  SHA-256s, and the third-party packages pinned for the CLI and the runtimes, the
  TypeScript runtime's from its npm lockfile, if the release holds it.
"""

import json
import re
import sys
from pathlib import Path
from typing import cast

RELEASE = Path(__file__).resolve().parent / "release"
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

# A pinned requirement as `uv export` writes it: its name and version, at the start
# of a line.
_PINNED = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", re.MULTILINE)


class NotARelease(Exception):
    """`release/` doesn't name a release. The message says why."""


def version(directory: Path | None = None) -> str | None:
    """The version `release/invarith.json` names, or None without `release/`."""
    named = (directory or RELEASE) / "invarith.json"
    if not named.exists():
        return None
    found: object = json.loads(named.read_text())
    given = (
        cast(dict[str, object], found).get("version")
        if isinstance(found, dict)
        else None
    )
    if not isinstance(given, str) or not VERSION.fullmatch(given):
        raise NotARelease("release/invarith.json's version isn't X.Y.Z.")
    return given


def notes(directory: Path | None = None) -> str:
    """The release's notes, in GitHub's Markdown."""
    directory = directory or RELEASE
    at = version(directory)
    if at is None:
        raise NotARelease("There's no release/invarith.json.")
    named = json.loads((directory / "invarith.json").read_text())
    packages = (
        named["packages"]
        | named.get("typescript_runtime", {})
        | named.get("java_runtime", {})
    )
    lines = [
        f"invarith/action v{at} runs invarith {at}: it downloads invarith's "
        "packages below, checks each against its SHA-256, and installs them with "
        "the third-party packages pinned below, each checked against its hashes.",
        "",
        "## invarith's packages",
        "",
        "| Package | SHA-256 |",
        "|---|---|",
        *(f"| `{name}` | `{sha}` |" for name, sha in sorted(packages.items())),
    ]
    for title, name in (
        ("Pinned for the CLI and the verifier", "cli.txt"),
        ("Pinned for the Python harness runtime", "runtime.txt"),
    ):
        pinned = _PINNED.findall((directory / name).read_text())
        lines += [
            "",
            f"## {title}",
            "",
            "| Package | Version |",
            "|---|---|",
            *(f"| `{package}` | `{at_version}` |" for package, at_version in pinned),
        ]
    lock = directory / "typescript-runtime-lock.json"
    if lock.exists():
        lines += [
            "",
            "## Pinned for the TypeScript runtime",
            "",
            "npm installs each one on the platforms it's for.",
            "",
            "| Package | Version | Platforms |",
            "|---|---|---|",
            *(f"| `{n}` | `{v}` | {where} |" for n, v, where in _locked(lock)),
        ]
    return "\n".join(lines) + "\n"


def _locked(lock: Path) -> list[tuple[str, str, str]]:
    """Each npm package the lockfile pins: its name, its version, and the platforms
    it's for."""
    packages = cast(
        dict[str, dict[str, object]], json.loads(lock.read_text())["packages"]
    )
    found: list[tuple[str, str, str]] = []
    for path, entry in packages.items():
        if not path:
            continue  # the root package, which names the runtime's dependencies
        name = path.rsplit("node_modules/", 1)[-1]
        systems = cast(list[str], entry.get("os", []))
        processors = cast(list[str], entry.get("cpu", []))
        where = " ".join([*systems, *processors]) or "all"
        found.append((name, str(entry["version"]), where))
    return sorted(found)


def main(argv: list[str]) -> int:
    try:
        match argv:
            case ["version"]:
                print(version() or "")
            case ["notes"]:
                print(notes(), end="")
            case _:
                print("Usage: release_notes.py version | notes", file=sys.stderr)
                return 2
    except NotARelease as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
