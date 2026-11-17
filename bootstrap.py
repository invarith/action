"""invarith's CI step, after bootstrap.sh has fetched uv: downloads the tools of the
invarith release this action release names, checks them, installs them beside the
project's environment, and runs `invarith check` (README.md; invarith's decisions D10,
D13 and D55).

uv runs it on a Python it provides, with Python's standard library only. In order:

1. **Which event.** Under `pull_request_target`, it refuses to run: that event runs
   with the base repository's permissions, so it would run a fork's code with them. A
   pull request from a fork exits here, with a notice, before asking for a token:
   GitHub gives a fork's workflows none, and invarith's App posts a neutral check run
   saying the pull request wasn't verified.
2. **The release** this action release names, from `release/`: `invarith.json`, the
   invarith release's version and each package's SHA-256, and `cli.txt` and
   `runtime.txt`, the third-party packages the tools need, pinned with their hashes.
3. **The job's OIDC token,** for invarith's audience, and with it, the Run API's
   short-lived links to the release's packages (`GET /v1/releases/{version}`).
4. **The packages,** each downloaded and checked against the SHA-256 the release
   lists. One the Run API doesn't offer, or lists with another SHA-256, stops here.
5. **The installs,** under `$RUNNER_TEMP/invarith`, never into the project's
   environment:
   - the CLI, the verifier, the core and the Python adapter in a virtual environment
     of their own, on the Python the CLI needs;
   - the Python harness runtime and its pinned packages in a directory of their own,
     for the project's interpreter, which `invarith check --tools` names.

   The third-party packages are installed from the release's lists with
   `--require-hashes`, and invarith's wheels with `--no-deps`, so nothing is
   resolved from a package index.
6. **`invarith check`,** with the project, its interpreter, the runtime's directory
   and the run's time, on the job's own PATH, which bootstrap.sh keeps, since uv puts
   its own Python first on the PATH of what it runs. Its exit code is the step's.

bootstrap.sh finds the project's interpreter on the job's PATH, for the same reason,
and gives its absolute path.

A token, and a download link, is never printed. It exits with 2 when the tools can't
be had, with why.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TextIO, cast

# The audience the Run API accepts the job's OIDC token for.
AUDIENCE = "https://invarith.ai"

# The Python the CLI's environment is created on.
CLI_PYTHON = "3.15"

# Where plain `http` is allowed for the server: this machine, as for a test stack.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# How long to wait for an answer, in seconds.
TIMEOUT = 60

RELEASE = "invarith.json"
CLI_REQUIREMENTS = "cli.txt"
RUNTIME_REQUIREMENTS = "runtime.txt"
RUNTIME_WHEEL = "invarith_runtime-"


class Stop(Exception):
    """The step can't go on. The message says why, for the person."""


@dataclass(frozen=True)
class Answer:
    status: int
    body: bytes

    def json(self) -> dict[str, Any]:
        try:
            found: object = json.loads(self.body)
        except ValueError:
            return {}
        return cast(dict[str, Any], found) if isinstance(found, dict) else {}


# Sends one GET request: the URL and the headers.
type Fetch = Callable[[str, Mapping[str, str]], Answer]
# Runs one command, with an environment if given, and gives its exit code.
type Run = Callable[[Sequence[str], Mapping[str, str] | None], int]


@dataclass(frozen=True)
class Release:
    """What this action release names: the invarith release's version, each
    package's name and SHA-256, and the pinned third-party packages' lists."""

    version: str
    packages: dict[str, str]
    cli_requirements: Path
    runtime_requirements: Path


@dataclass(frozen=True)
class Inputs:
    server: str
    minutes: str
    project: Path
    python: str


def fetch(url: str, headers: Mapping[str, str]) -> Answer:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return Answer(response.status, response.read())
    except urllib.error.HTTPError as error:
        return Answer(error.code, error.read())
    except (urllib.error.URLError, OSError) as error:
        raise Stop(f"{_host(url)} couldn't be reached: {_reason(error)}") from None


def run(command: Sequence[str], environment: Mapping[str, str] | None) -> int:
    return subprocess.run(
        list(command),
        env=None if environment is None else dict(environment),
        check=False,
    ).returncode


def main(
    environment: Mapping[str, str] = os.environ,
    *,
    send: Fetch = fetch,
    execute: Run = run,
    out: TextIO = sys.stdout,
) -> int:
    try:
        return bootstrap(environment, send=send, execute=execute, out=out)
    except Stop as error:
        print(f"::error title=invarith::{_escaped(str(error))}", file=out)
        return 2


def bootstrap(
    environment: Mapping[str, str], *, send: Fetch, execute: Run, out: TextIO
) -> int:
    if not from_this_repository(environment, out):
        return 0
    action = Path(_required(environment, "GITHUB_ACTION_PATH"))
    release = read_release(action / "release")
    inputs = read_inputs(environment)
    home = Path(_required(environment, "RUNNER_TEMP")) / "invarith"
    home.mkdir(parents=True, exist_ok=True)
    token = job_token(environment, send)
    links = release_links(inputs.server, release, token, send)
    wheels = download(release, links, home / "packages", send)
    uv = environment.get("UV") or shutil.which("uv") or "uv"
    tools = install(uv, release, wheels, home, inputs.python, execute)
    print(f"invarith {release.version} is installed; checking.", file=out)
    job = dict(environment)
    job["PATH"] = environment.get("INVARITH_JOB_PATH") or environment.get("PATH", "")
    return execute(
        [
            str(home / "cli" / "bin" / "invarith"),
            "--server",
            inputs.server,
            "check",
            "--project",
            str(inputs.project),
            "--python",
            inputs.python,
            "--tools",
            str(tools),
            "--minutes",
            inputs.minutes,
        ],
        job,
    )


def from_this_repository(environment: Mapping[str, str], out: TextIO) -> bool:
    """Whether to go on: not for `pull_request_target`, which it refuses, nor for a
    pull request from a fork, which it says it doesn't verify."""
    event = environment.get("GITHUB_EVENT_NAME", "")
    if event == "pull_request_target":
        raise Stop(
            "invarith/action must never run under pull_request_target, which would "
            "run a fork's code with this repository's permissions. Run it under "
            "pull_request instead."
        )
    if event != "pull_request":
        return True
    payload = _event(environment)
    pull = _fields(payload.get("pull_request"))
    head = pull.get("head")
    base_repository = _fields(_fields(pull.get("base")).get("repo")).get("id")
    head_repository = _fields(_fields(head).get("repo")).get("id")
    if base_repository is None:
        raise Stop("The pull request's event names no base repository.")
    if head_repository is not None and head_repository == base_repository:
        return True
    print(
        "::notice title=invarith::This pull request comes from a fork, so invarith "
        "doesn't verify it: GitHub gives a fork's workflows no OIDC token. The "
        "default-branch run checks the code once it merges.",
        file=out,
    )
    return False


def read_release(directory: Path) -> Release:
    """The invarith release this action release names, from `release/`."""
    named = directory / RELEASE
    if not named.is_file():
        raise Stop(
            "This commit of invarith/action names no invarith release. Pin the action "
            "to a release tag, such as invarith/action@v0.6.0, or to a release's "
            "commit."
        )
    try:
        found = _fields(json.loads(named.read_text()))
    except ValueError:
        raise Stop(f"release/{RELEASE} isn't JSON.") from None
    version = found.get("version")
    listed = _fields(found.get("packages"))
    packages = {
        name: sha
        for name, sha in listed.items()
        if isinstance(sha, str) and len(sha) == 64
    }
    if not isinstance(version, str) or not packages or len(packages) != len(listed):
        raise Stop(f"release/{RELEASE} needs a version and each package's SHA-256.")
    if sum(name.startswith(RUNTIME_WHEEL) for name in packages) != 1:
        raise Stop(f"release/{RELEASE} needs one runtime package.")
    for requirements in (CLI_REQUIREMENTS, RUNTIME_REQUIREMENTS):
        if not (directory / requirements).is_file():
            raise Stop(f"release/{requirements} is missing.")
    return Release(
        version=version,
        packages=packages,
        cli_requirements=directory / CLI_REQUIREMENTS,
        runtime_requirements=directory / RUNTIME_REQUIREMENTS,
    )


def read_inputs(environment: Mapping[str, str]) -> Inputs:
    server = environment.get("INVARITH_SERVER", "").rstrip("/")
    parsed = urllib.parse.urlsplit(server)
    local = parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS
    if not parsed.hostname or not (parsed.scheme == "https" or local):
        raise Stop(
            "The server input must be an https address, or http on this machine."
        )
    minutes = environment.get("INVARITH_MINUTES", "15")
    try:
        if not float(minutes) > 0:
            raise ValueError
    except ValueError:
        raise Stop("The minutes input must be a number of minutes above 0.") from None
    given = environment.get("INVARITH_PYTHON", "python")
    path = environment.get("INVARITH_JOB_PATH") or environment.get("PATH")
    python = given if os.path.isabs(given) else shutil.which(given, path=path)
    if python is None or not os.access(python, os.X_OK) or os.path.isdir(python):
        raise Stop(
            f"The project's interpreter, {given}, wasn't found. Set the python input "
            "to the interpreter the project's dependencies are installed for."
        )
    project = Path(environment.get("INVARITH_PROJECT", ".")).resolve()
    return Inputs(server=server, minutes=minutes, project=project, python=python)


def job_token(environment: Mapping[str, str], send: Fetch) -> str:
    """A new OIDC token for the job, for invarith's audience."""
    url = environment.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    bearer = environment.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not bearer:
        raise Stop(
            "GitHub gave this job no OIDC token. The job needs `permissions: "
            "id-token: write`."
        )
    joiner = "&" if "?" in url else "?"
    answer = send(
        f"{url}{joiner}audience={urllib.parse.quote(AUDIENCE, safe='')}",
        {"Authorization": f"Bearer {bearer}"},
    )
    value = answer.json().get("value")
    if answer.status != 200 or not isinstance(value, str) or not value:
        raise Stop(f"GitHub's token service answered {answer.status} with no token.")
    return value


def release_links(
    server: str, release: Release, token: str, send: Fetch
) -> dict[str, str]:
    """Each of the release's packages' download link, from the Run API."""
    answer = send(
        f"{server}/v1/releases/{urllib.parse.quote(release.version, safe='')}",
        {"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    body = answer.json()
    if answer.status != 200:
        message = body.get("message")
        said = f": {message}" if isinstance(message, str) and message else ""
        raise Stop(
            f"invarith's server answered {answer.status} for release "
            f"{release.version}{said}"
        )
    offered: dict[str, tuple[str, str]] = {}
    for item in body.get("files", []):
        found = _fields(item)
        name, sha, url = found.get("name"), found.get("sha256"), found.get("url")
        if isinstance(name, str) and isinstance(sha, str) and isinstance(url, str):
            offered[name] = (sha, url)
    links: dict[str, str] = {}
    for name, sha in release.packages.items():
        if name not in offered:
            raise Stop(f"invarith's server doesn't offer {name}.")
        if offered[name][0] != sha:
            raise Stop(
                f"invarith's server lists another SHA-256 for {name} than this "
                "action release does."
            )
        links[name] = offered[name][1]
    return links


def download(
    release: Release, links: Mapping[str, str], directory: Path, send: Fetch
) -> list[Path]:
    """Each package, downloaded and checked against its SHA-256."""
    directory.mkdir(parents=True, exist_ok=True)
    found: list[Path] = []
    for name, sha in sorted(release.packages.items()):
        answer = send(links[name], {})
        if answer.status != 200:
            raise Stop(f"{name}'s download answered {answer.status}.")
        if hashlib.sha256(answer.body).hexdigest() != sha:
            raise Stop(f"{name} doesn't match its SHA-256.")
        path = directory / Path(name).name
        path.write_bytes(answer.body)
        found.append(path)
    return found


def install(
    uv: str,
    release: Release,
    wheels: Sequence[Path],
    home: Path,
    python: str,
    execute: Run,
) -> Path:
    """Install the tools, and give the runtime's directory."""
    environment = home / "cli"
    tools = home / "tools"
    runtime = [w for w in wheels if w.name.startswith(RUNTIME_WHEEL)]
    others = [str(w) for w in wheels if not w.name.startswith(RUNTIME_WHEEL)]
    cli_python = str(environment / "bin" / "python")
    steps: list[tuple[str, list[str]]] = [
        (
            "the CLI's environment",
            [uv, "venv", "--no-config", "--python", CLI_PYTHON, str(environment)],
        ),
        (
            "the CLI's pinned packages",
            [
                uv,
                "pip",
                "install",
                "--no-config",
                "--python",
                cli_python,
                "--require-hashes",
                "-r",
                str(release.cli_requirements),
            ],
        ),
        (
            "the CLI",
            [
                uv,
                "pip",
                "install",
                "--no-config",
                "--python",
                cli_python,
                "--no-deps",
                *others,
            ],
        ),
        (
            "the runtime's pinned packages",
            [
                uv,
                "pip",
                "install",
                "--no-config",
                "--python",
                python,
                "--target",
                str(tools),
                "--require-hashes",
                "-r",
                str(release.runtime_requirements),
            ],
        ),
        (
            "the runtime",
            [
                uv,
                "pip",
                "install",
                "--no-config",
                "--python",
                python,
                "--target",
                str(tools),
                "--no-deps",
                str(runtime[0]),
            ],
        ),
    ]
    for what, command in steps:
        if execute(command, None) != 0:
            raise Stop(f"uv couldn't install {what}; its message is above.")
    return tools


def _event(environment: Mapping[str, str]) -> dict[str, Any]:
    path = environment.get("GITHUB_EVENT_PATH", "")
    try:
        return _fields(json.loads(Path(path).read_text()))
    except OSError, ValueError:
        raise Stop("The workflow's event couldn't be read.") from None


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value:
        _missing(name)
    return value


def _missing(name: str) -> NoReturn:
    raise Stop(f"{name} isn't set: invarith/action runs only in a GitHub Actions job.")


def _escaped(text: str) -> str:
    """Text for a workflow command, which GitHub reads to the end of the line."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _fields(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or "The server"


def _reason(error: Exception) -> str:
    reason = getattr(error, "reason", None)
    return str(reason) if reason is not None else type(error).__name__


if __name__ == "__main__":
    sys.exit(main())
