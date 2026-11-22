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
   A release that holds the TypeScript runtime also lists its npm package in
   `invarith.json`, under `typescript_runtime`, and pins its third-party packages in
   `typescript-runtime-lock.json`, an npm lockfile (invarith's decision D82).
3. **The inputs:** the server, the run's time, the project, and its interpreter and
   Node.js. The project is a TypeScript project if it has a `package.json` and the
   release holds the TypeScript runtime (D84). A TypeScript project needs its
   Node.js, and needs no interpreter if the `python` input is its default and isn't
   found.
4. **The job's OIDC token,** for invarith's audience, and with it, the Run API's
   short-lived links to the release's packages (`GET /v1/releases/{version}`).
5. **The packages,** each downloaded and checked against the SHA-256 the release
   lists. One the Run API doesn't offer, or lists with another SHA-256, stops here.
6. **The installs,** under `$RUNNER_TEMP/invarith`, never into the project's
   environment:
   - the CLI, the verifier, the core and the adapters in a virtual environment of
     their own, on the Python the CLI needs;
   - the Python harness runtime and its pinned packages in a directory of their own,
     for the project's interpreter, which `invarith check --tools` names;
   - for a TypeScript project, the TypeScript runtime's pinned packages in a
     directory of their own, installed by the npm beside the project's Node.js from
     the release's lockfile, with `npm ci --ignore-scripts`, and the runtime's
     package unpacked beside them, in `node_modules/@invarith/runtime`, which
     `invarith check --typescript-tools` names.

   The third-party packages are installed from the release's lists with
   `--require-hashes`, or from its lockfile, which pins each npm package with its
   integrity, and invarith's wheels with `--no-deps`, so nothing is resolved from a
   package index.
7. **`invarith check`,** with the project, its interpreter and Node.js, the runtimes'
   directories and the run's time, on the job's own PATH, which bootstrap.sh keeps,
   since uv puts its own Python first on the PATH of what it runs. Its exit code is
   the step's.

bootstrap.sh finds the project's interpreter on the job's PATH, for the same reason,
and gives its absolute path, or leaves the input as it is if there's none, for this
script to say so.

A token, and a download link, is never printed. It exits with 2 when the tools can't
be had, with why.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
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
TYPESCRIPT_LOCK = "typescript-runtime-lock.json"
TYPESCRIPT_PACKAGE = "invarith-typescript-runtime-"

# The `python` and `node` inputs' defaults, as action.yml gives them.
DEFAULT_PYTHON = "python"
DEFAULT_NODE = "node"

# What makes a project a TypeScript project, in its directory (invarith's D84).
PACKAGE_JSON = "package.json"

# The job's means of asking for OIDC tokens, which npm isn't given.
TOKEN_VARIABLES = ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")


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
    package's name and SHA-256, and the pinned third-party packages' lists; and, if
    it holds the TypeScript runtime, its npm package, with its SHA-256, and the
    lockfile that pins its third-party packages."""

    version: str
    packages: dict[str, str]
    cli_requirements: Path
    runtime_requirements: Path
    typescript: tuple[str, str] | None = None
    typescript_lock: Path | None = None


@dataclass(frozen=True)
class Inputs:
    """The action's inputs. `python` is the project's interpreter, or None if it
    wasn't found; `python_given` and `node` are the inputs as given."""

    server: str
    minutes: str
    project: Path
    python: str | None
    python_given: str
    node: str


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
    typescript = release.typescript is not None and typescript_project(inputs.project)
    python = project_python(inputs, typescript, out)
    node = project_node(inputs.node, environment) if typescript else None
    home = Path(_required(environment, "RUNNER_TEMP")) / "invarith"
    home.mkdir(parents=True, exist_ok=True)
    token = job_token(environment, send)
    wanted = files(release, typescript)
    links = release_links(inputs.server, release.version, wanted, token, send)
    downloaded = download(wanted, links, home / "packages", send)
    uv = environment.get("UV") or shutil.which("uv") or "uv"
    tools = install(uv, release, downloaded, home, python, execute)
    command = [
        str(home / "cli" / "bin" / "invarith"),
        "--server",
        inputs.server,
        "check",
        "--project",
        str(inputs.project),
    ]
    if python is not None and tools is not None:
        command += ["--python", python, "--tools", str(tools)]
    if node is not None and release.typescript is not None:
        package = downloaded[release.typescript[0]]
        typescript_tools = install_typescript(
            release, package, home, node, environment, execute
        )
        command += ["--node", node, "--typescript-tools", str(typescript_tools)]
    command += ["--minutes", inputs.minutes]
    print(f"invarith {release.version} is installed; checking.", file=out)
    job = dict(environment)
    job["PATH"] = _job_path(environment)
    return execute(command, job)


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
    typescript: tuple[str, str] | None = None
    lock: Path | None = None
    if "typescript_runtime" in found:
        typescript = _typescript_package(found["typescript_runtime"])
        lock = directory / TYPESCRIPT_LOCK
        if not lock.is_file():
            raise Stop(f"release/{TYPESCRIPT_LOCK} is missing.")
    return Release(
        version=version,
        packages=packages,
        cli_requirements=directory / CLI_REQUIREMENTS,
        runtime_requirements=directory / RUNTIME_REQUIREMENTS,
        typescript=typescript,
        typescript_lock=lock,
    )


def _typescript_package(listed: object) -> tuple[str, str]:
    """The TypeScript runtime's npm package, and its SHA-256, as `invarith.json` lists
    it under `typescript_runtime`."""
    entries = list(_fields(listed).items())
    if len(entries) == 1:
        name, sha = entries[0]
        named = name.startswith(TYPESCRIPT_PACKAGE) and name.endswith(".tgz")
        if named and "/" not in name and isinstance(sha, str) and len(sha) == 64:
            return name, sha
    raise Stop(
        f"release/{RELEASE} needs the TypeScript runtime's npm package, and its "
        "SHA-256, under typescript_runtime."
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
    given = environment.get("INVARITH_PYTHON") or DEFAULT_PYTHON
    project = Path(environment.get("INVARITH_PROJECT", ".")).resolve()
    return Inputs(
        server=server,
        minutes=minutes,
        project=project,
        python=_executable(given, _job_path(environment)),
        python_given=given,
        node=environment.get("INVARITH_NODE") or DEFAULT_NODE,
    )


def typescript_project(project: Path) -> bool:
    """Whether the project is a TypeScript project, to invarith: one with a
    `package.json` in its directory (invarith's D84)."""
    return (project / PACKAGE_JSON).is_file()


def project_python(inputs: Inputs, typescript: bool, out: TextIO) -> str | None:
    """The project's interpreter. A TypeScript project needs none if the python input
    is its default and isn't found: Python's runtime isn't installed, and a notice
    says so."""
    if inputs.python is not None:
        return inputs.python
    if typescript and inputs.python_given == DEFAULT_PYTHON:
        print(
            "::notice title=invarith::No python was found, so invarith checks this "
            "TypeScript project without its Python runtime, and can't check a claim "
            "bound in Python. To check one, set the python input to the project's "
            "interpreter.",
            file=out,
        )
        return None
    raise Stop(
        f"The project's interpreter, {inputs.python_given}, wasn't found. Set the "
        "python input to the interpreter the project's dependencies are installed "
        "for."
    )


def project_node(given: str, environment: Mapping[str, str]) -> str:
    """The project's Node.js, for a TypeScript project: the node input, found on the
    job's PATH if it's a name, and made absolute if it's a path."""
    node = _executable(given, _job_path(environment))
    if node is None:
        raise Stop(
            f"The project has a {PACKAGE_JSON}, so invarith checks it with its "
            f"TypeScript runtime, on the project's Node.js, but {given} wasn't found. "
            "Set up Node.js before the invarith step, such as with "
            "actions/setup-node, or set the node input to the project's Node.js."
        )
    return node


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


def files(release: Release, typescript: bool) -> dict[str, str]:
    """The release's files to download, each with its SHA-256: its wheels, and, for a
    TypeScript project, the TypeScript runtime's npm package."""
    wanted = dict(release.packages)
    if typescript and release.typescript is not None:
        name, sha = release.typescript
        wanted[name] = sha
    return wanted


def release_links(
    server: str, version: str, wanted: Mapping[str, str], token: str, send: Fetch
) -> dict[str, str]:
    """Each wanted file's download link, from the Run API."""
    answer = send(
        f"{server}/v1/releases/{urllib.parse.quote(version, safe='')}",
        {"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    body = answer.json()
    if answer.status != 200:
        message = body.get("message")
        said = f": {message}" if isinstance(message, str) and message else ""
        raise Stop(
            f"invarith's server answered {answer.status} for release {version}{said}"
        )
    offered: dict[str, tuple[str, str]] = {}
    for item in body.get("files", []):
        found = _fields(item)
        name, sha, url = found.get("name"), found.get("sha256"), found.get("url")
        if isinstance(name, str) and isinstance(sha, str) and isinstance(url, str):
            offered[name] = (sha, url)
    links: dict[str, str] = {}
    for name, sha in wanted.items():
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
    wanted: Mapping[str, str], links: Mapping[str, str], directory: Path, send: Fetch
) -> dict[str, Path]:
    """Each wanted file, downloaded and checked against its SHA-256, by name."""
    directory.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}
    for name, sha in sorted(wanted.items()):
        answer = send(links[name], {})
        if answer.status != 200:
            raise Stop(f"{name}'s download answered {answer.status}.")
        if hashlib.sha256(answer.body).hexdigest() != sha:
            raise Stop(f"{name} doesn't match its SHA-256.")
        path = directory / Path(name).name
        path.write_bytes(answer.body)
        found[name] = path
    return found


def install(
    uv: str,
    release: Release,
    downloaded: Mapping[str, Path],
    home: Path,
    python: str | None,
    execute: Run,
) -> Path | None:
    """Install the CLI, and, for the project's interpreter, if there's one, the
    Python runtime, and give the runtime's directory."""
    environment = home / "cli"
    tools = home / "tools"
    wheels = [downloaded[name] for name in sorted(release.packages)]
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
    ]
    if python is not None:
        steps += _python_runtime(uv, release, runtime[0], tools, python)
    for what, command in steps:
        if execute(command, None) != 0:
            raise Stop(f"uv couldn't install {what}; its message is above.")
    return tools if python is not None else None


def _python_runtime(
    uv: str, release: Release, runtime: Path, tools: Path, python: str
) -> list[tuple[str, list[str]]]:
    """The Python runtime's installs, for the project's interpreter."""
    return [
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
                str(runtime),
            ],
        ),
    ]


def install_typescript(
    release: Release,
    package: Path,
    home: Path,
    node: str,
    environment: Mapping[str, str],
    execute: Run,
) -> Path:
    """Install the TypeScript runtime, for the project's Node.js, and give its
    directory (invarith's D82 and D84): its pinned packages by the npm beside the
    project's Node.js, from the release's lockfile, with `npm ci`, which installs
    exactly those, checks each one's integrity, and, with `--ignore-scripts`, runs
    none of their code; then the runtime's package, unpacked beside them, where `npm
    install` puts a package, once `npm ci` has run, since it empties `node_modules`
    first."""
    assert release.typescript_lock is not None
    tools = home / "typescript"
    tools.mkdir(parents=True, exist_ok=True)
    locked = release.typescript_lock.read_text()
    (tools / "package-lock.json").write_text(locked)
    (tools / PACKAGE_JSON).write_text(json.dumps(_package_json(locked), indent=2))
    path = os.pathsep.join([os.path.dirname(node), _job_path(environment)])
    npm = shutil.which("npm", path=path)
    if npm is None:
        raise Stop(
            f"npm wasn't found beside the project's Node.js, {node}, or on the job's "
            "PATH, so the TypeScript runtime can't be installed."
        )
    npm_environment = {
        name: value
        for name, value in environment.items()
        if name not in TOKEN_VARIABLES
    }
    npm_environment |= {
        "PATH": path,
        "npm_config_cache": str(home / "npm"),
        "npm_config_update_notifier": "false",
    }
    command = [
        npm,
        "ci",
        "--prefix",
        str(tools),
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]
    if execute(command, npm_environment) != 0:
        raise Stop(
            "npm couldn't install the TypeScript runtime's pinned packages; its "
            "message is above."
        )
    unpack(package, tools / "node_modules" / "@invarith" / "runtime")
    return tools


def _package_json(locked: str) -> dict[str, Any]:
    """The `package.json` the lockfile's root package implies: its name and its
    dependencies, which `npm ci` checks the lockfile against."""
    try:
        root = _fields(_fields(json.loads(locked).get("packages")).get(""))
    except ValueError, AttributeError:
        root = {}
    dependencies = _fields(root.get("dependencies"))
    if not dependencies or not all(
        isinstance(version, str) for version in dependencies.values()
    ):
        raise Stop(
            f"release/{TYPESCRIPT_LOCK} isn't an npm lockfile whose root package "
            "names its dependencies."
        )
    return {
        "name": root.get("name") or "invarith-typescript-runtime-dependencies",
        "private": True,
        "dependencies": dependencies,
    }


def unpack(package: Path, into: Path) -> None:
    """The npm package's files and directories, which `npm pack` puts under
    `package/`, extracted into `into`, without that prefix. Anything else in it, such
    as a link, stops here."""
    into.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(package, "r:gz") as archive:
            members: list[tarfile.TarInfo] = []
            for member in archive.getmembers():
                name = member.name.removeprefix("./")
                if name in ("package", "package/") and member.isdir():
                    continue
                if not name.startswith("package/"):
                    raise Stop(f"{package.name} holds {member.name}, outside package/.")
                if not (member.isfile() or member.isdir()):
                    raise Stop(f"{package.name} holds {member.name}, which is a link.")
                member.name = name.removeprefix("package/")
                if member.name:
                    members.append(member)
            archive.extractall(into, members=members, filter="data")
    except (tarfile.TarError, OSError) as error:
        raise Stop(f"{package.name} couldn't be unpacked: {error}") from None


def _job_path(environment: Mapping[str, str]) -> str:
    """The job's own PATH, which bootstrap.sh keeps, since uv puts its own Python
    first on the PATH of what it runs."""
    return environment.get("INVARITH_JOB_PATH") or environment.get("PATH", "")


def _executable(given: str, path: str) -> str | None:
    """An executable the job names: a name, found on `path`, or a path, made absolute
    without following links, since a virtual environment's interpreter is one."""
    if os.sep in given:
        found: str | None = os.path.abspath(given)
    else:
        found = shutil.which(given, path=path)
    if found is None or not os.access(found, os.X_OK) or os.path.isdir(found):
        return None
    return found


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
