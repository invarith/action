"""bootstrap.py: which events it verifies, the release it names, the job's token, the
Run API's links, the downloads and their checks, the installs and `invarith check`,
for a Python project, a TypeScript one and a Java one, built with Gradle or Maven.

Most tests stand in for the network and for the commands it runs. The last ones run
it for real, with uv, with npm for a TypeScript project, and with the Java tools'
jars for a Java project, against a stand-in for the Run API on this machine that
serves small wheels, npm packages and jars built here, so they need no package
index. The CLI they install stands in for invarith's, recording how its setup and
its check were run, so no Java project is built."""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

import bootstrap
from bootstrap import AUDIENCE, Answer

VERSION = "0.6.0"
TOKEN_URL = "https://token.actions.example/token?run=1"
JOB_BEARER = "job-bearer"
OIDC = "oidc-token-value"
SERVER = "https://invarith.example"
BASE, FORK = 1296269, 5551212


# Packages


def wheel(
    directory: Path,
    name: str,
    files: Mapping[str, str],
    *,
    version: str = VERSION,
    scripts: Mapping[str, str] | None = None,
) -> Path:
    """A wheel, as an installer reads one: its files, its metadata and its RECORD."""
    info = f"{name}-{version}.dist-info"
    entries = dict(files)
    entries[f"{info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {name.replace('_', '-')}\nVersion: {version}\n"
    )
    entries[f"{info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )
    if scripts:
        entries[f"{info}/entry_points.txt"] = "[console_scripts]\n" + "".join(
            f"{script} = {target}\n" for script, target in scripts.items()
        )
    record: list[str] = []
    for path, content in entries.items():
        data = content.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
        record.append(f"{path},sha256={digest.rstrip(b'=').decode()},{len(data)}")
    record.append(f"{info}/RECORD,,")
    entries[f"{info}/RECORD"] = "\n".join(record) + "\n"
    path = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name_in, content in entries.items():
            archive.writestr(name_in, content)
    return path


# The CLI stands in for invarith's: it records how it was run, `invarith setup` beside
# the rest, and exits as asked, `invarith setup` with FAKE_SETUP_EXIT, or 0.
FAKE_CLI = """import json, os, sys

def main():
    record, exit = os.environ["FAKE_RECORD"], "FAKE_EXIT"
    if sys.argv[1:2] == ["setup"]:
        record, exit = record + ".setup", "FAKE_SETUP_EXIT"
    with open(record, "w") as out:
        json.dump(
            {
                "argv": sys.argv[1:],
                "path": os.environ["PATH"],
                "token": "ACTIONS_ID_TOKEN_REQUEST_URL" in os.environ,
            },
            out,
        )
    return int(os.environ.get(exit, "0"))
"""


def npm_package(path: Path, files: Mapping[str, str]) -> Path:
    """An npm package, as `npm pack` packs one: its files under `package/`."""
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def integrity(path: Path) -> str:
    """An npm lockfile's integrity for a package."""
    return (
        "sha512-"
        + base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode()
    )


def pinned(wheel_path: Path) -> str:
    """A pinned requirement on a wheel on this machine, in requirements format."""
    name = wheel_path.name.split("-")[0].replace("_", "-")
    return (
        f"{name} @ {wheel_path.resolve().as_uri()} \\\n"
        f"    --hash=sha256:{sha256(wheel_path)}\n"
    )


@dataclass
class Release:
    """An action release, with the packages it names, built here: its wheels, the
    TypeScript runtime's npm package, if it holds one, and the Java tools' jars, if it
    holds them."""

    action: Path
    packages: dict[str, Path]
    typescript: dict[str, Path] = field(default_factory=dict[str, Path])
    java: dict[str, Path] = field(default_factory=dict[str, Path])

    @property
    def offered(self) -> dict[str, Path]:
        """What the Run API offers of the release."""
        return self.packages | self.typescript | self.java


def release(tmp_path: Path) -> Release:
    """The action's directory, with `release/` naming a release of small wheels: the
    core, the CLI, the runtime, and one third-party package each list pins."""
    built = tmp_path / "built"
    built.mkdir()
    packages = {
        p.name: p
        for p in (
            wheel(built, "invarith_core", {"invarith_core/__init__.py": ""}),
            wheel(
                built,
                "invarith_cli",
                {"invarith_cli/__init__.py": "", "invarith_cli/main.py": FAKE_CLI},
                scripts={"invarith": "invarith_cli.main:main"},
            ),
            wheel(built, "invarith_runtime", {"invarith_runtime/__init__.py": ""}),
        )
    }
    dependency = wheel(
        built, "pinned_dep", {"pinned_dep/__init__.py": "VALUE = 1\n"}, version="1.0"
    )
    action = tmp_path / "action"
    directory = action / "release"
    directory.mkdir(parents=True)
    (directory / "invarith.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "packages": {n: sha256(p) for n, p in packages.items()},
            }
        )
    )
    (directory / "cli.txt").write_text(pinned(dependency))
    (directory / "runtime.txt").write_text(pinned(dependency))
    return Release(action, packages)


TYPESCRIPT_RUNTIME = f"invarith-typescript-runtime-{VERSION}.tgz"

# The pinned npm package's install script, which writes a file if it runs.
INSTALL_SCRIPT = "node -e \"require('fs').writeFileSync('install-script-ran', '')\""


def with_typescript(given: Release, tmp_path: Path) -> Release:
    """The release, holding the TypeScript runtime: its npm package, built here, under
    `typescript_runtime`, and `typescript-runtime-lock.json`, which pins one npm
    package, a tarball on this machine, with an install script."""
    built = tmp_path / "built"
    runtime = npm_package(
        built / TYPESCRIPT_RUNTIME,
        {
            "package.json": json.dumps(
                {
                    "name": "@invarith/runtime",
                    "version": VERSION,
                    "type": "module",
                    "exports": {".": "./dist/index.js"},
                    "dependencies": {"pinned-npm-dep": "1.0.0"},
                }
            ),
            "dist/index.js": f'export const VERSION = "{VERSION}";\n',
        },
    )
    dependency = npm_package(
        built / "pinned-npm-dep-1.0.0.tgz",
        {
            "package.json": json.dumps(
                {
                    "name": "pinned-npm-dep",
                    "version": "1.0.0",
                    "type": "module",
                    "main": "index.js",
                    "scripts": {"install": INSTALL_SCRIPT},
                }
            ),
            "index.js": "export const VALUE = 1;\n",
        },
    )
    directory = given.action / "release"
    named = json.loads((directory / "invarith.json").read_text())
    named["typescript_runtime"] = {TYPESCRIPT_RUNTIME: sha256(runtime)}
    (directory / "invarith.json").write_text(json.dumps(named))
    root = {"pinned-npm-dep": "1.0.0"}
    lock = {
        "name": "invarith-typescript-runtime-dependencies",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {
                "name": "invarith-typescript-runtime-dependencies",
                "dependencies": root,
            },
            "node_modules/pinned-npm-dep": {
                "version": "1.0.0",
                "resolved": f"file:{dependency.resolve()}",
                "integrity": integrity(dependency),
                "hasInstallScript": True,
            },
        },
    }
    (directory / "typescript-runtime-lock.json").write_text(json.dumps(lock, indent=2))
    return Release(
        given.action, given.packages, {TYPESCRIPT_RUNTIME: runtime}, given.java
    )


RUNTIME_JAR = f"invarith-runtime-{VERSION}.jar"
MAVEN_EXTENSION_JAR = f"invarith-maven-extension-{VERSION}.jar"
# Java's markers, as invarith's Java adapter names them: the Gradle and Maven build
# files.
JAVA_MARKERS = [
    "settings.gradle",
    "settings.gradle.kts",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
]


def with_java(
    given: Release, tmp_path: Path, markers: Mapping[str, list[str]] | None = None
) -> Release:
    """The release, holding the Java tools: their jars, built here, under
    `java_runtime`, and each language's markers, under `markers`: by default, Java's,
    and TypeScript's if the release holds its runtime."""
    built = tmp_path / "built"
    jars: dict[str, Path] = {}
    for name in (RUNTIME_JAR, MAVEN_EXTENSION_JAR):
        (built / name).write_bytes(f"the {name} jar".encode())
        jars[name] = built / name
    directory = given.action / "release"
    named = json.loads((directory / "invarith.json").read_text())
    named["java_runtime"] = {name: sha256(path) for name, path in jars.items()}
    if markers is None:
        markers = {"java": JAVA_MARKERS}
        if given.typescript:
            markers = {**markers, "typescript": ["package.json"]}
    named["markers"] = dict(markers)
    (directory / "invarith.json").write_text(json.dumps(named))
    return Release(given.action, given.packages, given.typescript, jars)


def typescript_project(tmp_path: Path) -> Path:
    """A TypeScript project: one with a package.json."""
    project = tmp_path / "shop"
    project.mkdir()
    (project / "package.json").write_text('{"type": "module"}\n')
    return project


def java_project(tmp_path: Path, build: str) -> Path:
    """A small Java project, built with Gradle or Maven: its build files and a class."""
    project = tmp_path / "shop"
    (project / "src" / "main" / "java" / "shop").mkdir(parents=True)
    (project / "src" / "main" / "java" / "shop" / "Line.java").write_text(
        "package shop;\n\npublic record Line(long cents) {}\n"
    )
    if build == "gradle":
        (project / "settings.gradle.kts").write_text('rootProject.name = "shop"\n')
        (project / "build.gradle.kts").write_text("plugins { java }\n")
    else:
        (project / "pom.xml").write_text(
            "<project><modelVersion>4.0.0</modelVersion><groupId>shop</groupId>"
            "<artifactId>shop</artifactId><version>1</version></project>\n"
        )
    return project


def job_bin(tmp_path: Path, *names: str) -> Path:
    """A directory on the job's PATH, holding executables with these names."""
    directory = tmp_path / "job-bin"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        executable = directory / name
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
    return directory


# The network, stood in for


@dataclass
class Network:
    """GitHub's token service and invarith's server, as `send` reaches them."""

    release: Release
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list[Any])
    answers: dict[str, Answer] = field(default_factory=dict[str, Answer])

    def __call__(self, url: str, headers: Mapping[str, str]) -> Answer:
        self.calls.append((url, dict(headers)))
        if url in self.answers:
            return self.answers[url]
        if url.startswith(TOKEN_URL):
            assert url == f"{TOKEN_URL}&audience=https%3A%2F%2Finvarith.ai"
            assert headers["Authorization"] == f"Bearer {JOB_BEARER}"
            return Answer(200, json.dumps({"value": OIDC}).encode())
        if url == f"{SERVER}/v1/releases/{VERSION}":
            assert headers["Authorization"] == f"Bearer {OIDC}"
            files = [
                {
                    "name": name,
                    "sha256": sha256(path),
                    "size": path.stat().st_size,
                    "url": f"https://storage.example/{name}?signature=secret",
                }
                for name, path in self.release.offered.items()
            ]
            return Answer(
                200, json.dumps({"version": VERSION, "files": files}).encode()
            )
        prefix = "https://storage.example/"
        if url.startswith(prefix):
            name = url.removeprefix(prefix).split("?")[0]
            return Answer(200, self.release.offered[name].read_bytes())
        return Answer(404, b"{}")


@dataclass
class Commands:
    """The commands it runs, each answered with `exits`'s code, or 0, and the time
    limit each was given."""

    run: list[tuple[list[str], Mapping[str, str] | None]] = field(
        default_factory=list[Any]
    )
    exits: dict[str, int] = field(default_factory=dict[str, int])
    timeouts: list[float | None] = field(default_factory=list[float | None])

    def __call__(
        self,
        command: Sequence[str],
        environment: Mapping[str, str] | None,
        timeout: float | None = None,
    ) -> int:
        self.run.append((list(command), environment))
        self.timeouts.append(timeout)
        for mark, code in self.exits.items():
            if mark in command:
                return code
        return 0


def environment(tmp_path: Path, given: Release, **changes: str) -> dict[str, str]:
    """A push's job, with the action's inputs, as action.yml and bootstrap.sh give
    them."""
    event = tmp_path / "event.json"
    if not event.exists():
        event.write_text("{}")
    found: dict[str, str] = {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_ACTION_PATH": str(given.action),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "ACTIONS_ID_TOKEN_REQUEST_URL": TOKEN_URL,
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": JOB_BEARER,
        "INVARITH_SERVER": SERVER,
        "INVARITH_MINUTES": "15",
        "INVARITH_PROJECT": str(tmp_path),
        "INVARITH_PYTHON": sys.executable,
        "INVARITH_JOB_PATH": "/job/bin:/usr/bin",
        "PATH": "/uv/python/bin:/job/bin:/usr/bin",
        "UV": "uv",
    }
    found.update(changes)
    return found


def pull_request(tmp_path: Path, head: int | None) -> str:
    """A pull request event's payload, from `head`'s repository, or from one GitHub
    no longer has."""
    event = tmp_path / "event.json"
    repository = None if head is None else {"id": head, "full_name": "mona/shop"}
    event.write_text(
        json.dumps(
            {
                "pull_request": {
                    "number": 7,
                    "head": {"sha": "b" * 40, "repo": repository},
                    "base": {"ref": "main", "repo": {"id": BASE}},
                }
            }
        )
    )
    return str(event)


def bootstrapped(
    variables: Mapping[str, str], network: Network, commands: Commands
) -> tuple[int, str]:
    out = io.StringIO()
    code = bootstrap.main(variables, send=network, execute=commands, out=out)
    return code, out.getvalue()


@pytest.fixture
def given(tmp_path: Path) -> Release:
    return release(tmp_path)


# Which events


@pytest.mark.parametrize("head", [FORK, None], ids=["a fork", "a deleted fork"])
def test_a_pull_request_from_a_fork_isn_t_verified(
    tmp_path: Path, given: Release, head: int | None
) -> None:
    """It exits before asking for a token, which GitHub wouldn't give, with a
    notice."""
    variables = environment(
        tmp_path,
        given,
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_EVENT_PATH=pull_request(tmp_path, head),
    )
    network, commands = Network(given), Commands()
    code, said = bootstrapped(variables, network, commands)
    assert code == 0
    assert said.startswith(
        "::notice title=invarith::This pull request comes from a fork"
    )
    assert (network.calls, commands.run) == ([], [])


def test_a_pull_request_from_the_repository_itself(
    tmp_path: Path, given: Release
) -> None:
    variables = environment(
        tmp_path,
        given,
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_EVENT_PATH=pull_request(tmp_path, BASE),
    )
    commands = Commands()
    code, _ = bootstrapped(variables, Network(given), commands)
    assert code == 0
    assert "check" in commands.run[-1][0]


def test_pull_request_target_is_refused(tmp_path: Path, given: Release) -> None:
    variables = environment(tmp_path, given, GITHUB_EVENT_NAME="pull_request_target")
    network = Network(given)
    code, said = bootstrapped(variables, network, Commands())
    assert code == 2
    assert "must never run under pull_request_target" in said
    assert network.calls == []


def test_an_event_without_its_base_repository(tmp_path: Path, given: Release) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"repo": {"id": FORK}}}}))
    variables = environment(
        tmp_path, given, GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=str(event)
    )
    code, said = bootstrapped(variables, Network(given), Commands())
    assert code == 2
    assert "names no base repository" in said


# The release


def test_a_commit_that_names_no_release(tmp_path: Path, given: Release) -> None:
    shutil.rmtree(given.action / "release")
    code, said = bootstrapped(environment(tmp_path, given), Network(given), Commands())
    assert code == 2
    assert "names no invarith release" in said
    assert "invarith/action@vX.Y.Z" in said
    assert "invarith/action@v" in said


@pytest.mark.parametrize(
    "broken",
    ["not json", "no runtime", "a short checksum", "no cli.txt"],
)
def test_a_release_that_isn_t_whole(
    tmp_path: Path, given: Release, broken: str
) -> None:
    directory = given.action / "release"
    named = json.loads((directory / "invarith.json").read_text())
    if broken == "not json":
        (directory / "invarith.json").write_text("{")
    elif broken == "no runtime":
        named["packages"] = {
            n: s for n, s in named["packages"].items() if "runtime" not in n
        }
        (directory / "invarith.json").write_text(json.dumps(named))
    elif broken == "a short checksum":
        name = next(iter(named["packages"]))
        named["packages"][name] = "abc"
        (directory / "invarith.json").write_text(json.dumps(named))
    else:
        (directory / "cli.txt").unlink()
    code, said = bootstrapped(environment(tmp_path, given), Network(given), Commands())
    assert code == 2
    assert "::error title=invarith::release/" in said


# The inputs


@pytest.mark.parametrize(
    ("server", "allowed"),
    [
        ("https://invarith.example", True),
        ("http://localhost:8000", True),
        ("http://127.0.0.1:8000", True),
        ("http://invarith.example", False),
        ("invarith.example", False),
        ("", False),
    ],
)
def test_the_server_is_https_or_this_machine(
    tmp_path: Path, given: Release, server: str, allowed: bool
) -> None:
    variables = environment(tmp_path, given, INVARITH_SERVER=server)
    if allowed:
        assert bootstrap.read_inputs(variables).server == server
    else:
        with pytest.raises(bootstrap.Stop, match="https address"):
            bootstrap.read_inputs(variables)


@pytest.mark.parametrize("minutes", ["0", "-3", "soon"])
def test_minutes_above_0(tmp_path: Path, given: Release, minutes: str) -> None:
    variables = environment(tmp_path, given, INVARITH_MINUTES=minutes)
    with pytest.raises(bootstrap.Stop, match="minutes"):
        bootstrap.read_inputs(variables)


def test_an_interpreter_that_isn_t_there(tmp_path: Path, given: Release) -> None:
    variables = environment(
        tmp_path, given, INVARITH_PYTHON=str(tmp_path / "no-python")
    )
    code, said = bootstrapped(variables, Network(given), Commands())
    assert code == 2
    assert "wasn't found" in said


def test_an_interpreter_by_name_is_found_on_the_job_s_path(
    tmp_path: Path, given: Release
) -> None:
    """Not on the PATH uv gives what it runs, which has uv's own Python first."""
    job = tmp_path / "job-bin"
    job.mkdir()
    interpreter = job / "python-project"
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)
    variables = environment(
        tmp_path,
        given,
        INVARITH_PYTHON="python-project",
        INVARITH_JOB_PATH=str(job),
    )
    assert bootstrap.read_inputs(variables).python == str(interpreter)


# The token and the links


def test_no_oidc_token(tmp_path: Path, given: Release) -> None:
    variables = environment(tmp_path, given)
    del variables["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
    code, said = bootstrapped(variables, Network(given), Commands())
    assert code == 2
    assert "id-token: write" in said


def test_the_token_service_gives_none(tmp_path: Path, given: Release) -> None:
    network = Network(given)
    network.answers[f"{TOKEN_URL}&audience=https%3A%2F%2Finvarith.ai"] = Answer(
        500, b""
    )
    code, said = bootstrapped(environment(tmp_path, given), network, Commands())
    assert code == 2
    assert "token service answered 500" in said


def test_the_server_refuses(tmp_path: Path, given: Release) -> None:
    """Its message is passed on, escaped for the workflow command it's in."""
    network = Network(given)
    network.answers[f"{SERVER}/v1/releases/{VERSION}"] = Answer(
        403,
        json.dumps(
            {"error": "forbidden", "message": "No App installation.\n100% sure."}
        ).encode(),
    )
    code, said = bootstrapped(environment(tmp_path, given), network, Commands())
    assert code == 2
    assert said == (
        "::error title=invarith::invarith's server answered 403 for release 0.6.0: "
        "No App installation.%0A100%25 sure.\n"
    )


def test_a_package_the_server_doesn_t_offer(tmp_path: Path, given: Release) -> None:
    named = given.action / "release" / "invarith.json"
    found = json.loads(named.read_text())
    found["packages"]["invarith_verifier-0.6.0-py3-none-any.whl"] = "a" * 64
    named.write_text(json.dumps(found))
    code, said = bootstrapped(environment(tmp_path, given), Network(given), Commands())
    assert code == 2
    assert "doesn't offer invarith_verifier-0.6.0-py3-none-any.whl" in said


def test_the_server_lists_another_checksum(tmp_path: Path, given: Release) -> None:
    named = given.action / "release" / "invarith.json"
    found = json.loads(named.read_text())
    name = next(n for n in found["packages"] if n.startswith("invarith_core"))
    found["packages"][name] = "f" * 64
    named.write_text(json.dumps(found))
    code, said = bootstrapped(environment(tmp_path, given), Network(given), Commands())
    assert code == 2
    assert f"another SHA-256 for {name}" in said


def test_a_download_that_doesn_t_match(tmp_path: Path, given: Release) -> None:
    """The server's list matches the release, but the file downloaded doesn't."""
    network = Network(given)
    name = next(n for n in given.packages if n.startswith("invarith_cli"))
    network.answers[f"https://storage.example/{name}?signature=secret"] = Answer(
        200, b"something else"
    )
    commands = Commands()
    code, said = bootstrapped(environment(tmp_path, given), network, commands)
    assert code == 2
    assert f"{name} doesn't match its SHA-256" in said
    assert commands.run == []


def test_a_download_that_fails(tmp_path: Path, given: Release) -> None:
    network = Network(given)
    name = next(n for n in given.packages if n.startswith("invarith_core"))
    network.answers[f"https://storage.example/{name}?signature=secret"] = Answer(
        403, b""
    )
    code, said = bootstrapped(environment(tmp_path, given), network, Commands())
    assert code == 2
    assert f"{name}'s download answered 403" in said


def test_no_token_or_link_is_printed(tmp_path: Path, given: Release) -> None:
    network = Network(given)
    name = next(n for n in given.packages if n.startswith("invarith_core"))
    network.answers[f"https://storage.example/{name}?signature=secret"] = Answer(
        500, b""
    )
    _, said = bootstrapped(environment(tmp_path, given), network, Commands())
    for secret in (OIDC, JOB_BEARER, "signature=secret", "storage.example"):
        assert secret not in said


# The installs and the check


def test_what_it_installs_and_runs(tmp_path: Path, given: Release) -> None:
    """The CLI's environment on the CLI's Python, its pinned packages with hashes,
    invarith's wheels without dependencies; the runtime and its pinned packages for
    the project's interpreter, in a directory of their own; then the check, on the
    job's PATH."""
    commands = Commands()
    code, said = bootstrapped(environment(tmp_path, given), Network(given), commands)
    assert code == 0
    assert said == "invarith 0.6.0 is installed; checking.\n"
    home = tmp_path / "runner" / "invarith"
    cli_python = str(home / "cli" / "bin" / "python")
    release_dir = given.action / "release"
    packages = home / "packages"
    others = sorted(str(packages / n) for n in given.packages if "runtime" not in n)
    runtime = next(str(packages / n) for n in given.packages if "runtime" in n)
    tools = str(home / "tools")
    assert [command for command, _ in commands.run] == [
        ["uv", "venv", "--no-config", "--python", "3.15", str(home / "cli")],
        [
            "uv",
            "pip",
            "install",
            "--no-config",
            "--python",
            cli_python,
            "--require-hashes",
            "-r",
            str(release_dir / "cli.txt"),
        ],
        [
            "uv",
            "pip",
            "install",
            "--no-config",
            "--python",
            cli_python,
            "--no-deps",
            *others,
        ],
        [
            "uv",
            "pip",
            "install",
            "--no-config",
            "--python",
            sys.executable,
            "--target",
            tools,
            "--require-hashes",
            "-r",
            str(release_dir / "runtime.txt"),
        ],
        [
            "uv",
            "pip",
            "install",
            "--no-config",
            "--python",
            sys.executable,
            "--target",
            tools,
            "--no-deps",
            runtime,
        ],
        [
            str(home / "cli" / "bin" / "invarith"),
            "--server",
            SERVER,
            "check",
            "--project",
            str(tmp_path),
            "--python",
            sys.executable,
            "--tools",
            tools,
            "--minutes",
            "15",
        ],
    ]
    check_environment = commands.run[-1][1]
    assert check_environment is not None
    assert check_environment["PATH"] == "/job/bin:/usr/bin"


def test_an_install_that_fails(tmp_path: Path, given: Release) -> None:
    commands = Commands(exits={"--require-hashes": 1})
    code, said = bootstrapped(environment(tmp_path, given), Network(given), commands)
    assert code == 2
    assert "uv couldn't install the CLI's pinned packages" in said
    assert not any("check" in command for command, _ in commands.run)


def test_the_check_s_exit_code_is_the_step_s(tmp_path: Path, given: Release) -> None:
    commands = Commands(exits={"check": 1})
    code, _ = bootstrapped(environment(tmp_path, given), Network(given), commands)
    assert code == 1


# A TypeScript project


def typescript_job(tmp_path: Path, given: Release, **changes: str) -> dict[str, str]:
    """A push's job for a TypeScript project, with Node.js and npm on its PATH."""
    found = environment(
        tmp_path,
        given,
        INVARITH_PROJECT=str(typescript_project(tmp_path)),
        INVARITH_JOB_PATH=str(job_bin(tmp_path, "node", "npm")),
    )
    found.update(changes)
    return found


def test_a_typescript_project(tmp_path: Path, given: Release) -> None:
    """For a project with a package.json, the TypeScript runtime's npm package is
    downloaded and checked with the wheels; its pinned packages are installed by the
    npm beside the project's Node.js, from the release's lockfile, from the npm
    registry, with install scripts off, npm's cache beside them, and without the
    job's means of asking for OIDC tokens or its npm configuration; the runtime is
    unpacked beside them; and the check is given the Node.js and the runtime's
    directory."""
    given = with_typescript(given, tmp_path)
    variables = typescript_job(
        tmp_path,
        given,
        npm_config_registry="https://npm.example/",
        NPM_CONFIG_USERCONFIG=str(tmp_path / "setup-node.npmrc"),
    )
    network, commands = Network(given), Commands()
    code, said = bootstrapped(variables, network, commands)
    assert code == 0, said
    home = tmp_path / "runner" / "invarith"
    tools = home / "typescript"
    job = tmp_path / "job-bin"
    assert f"https://storage.example/{TYPESCRIPT_RUNTIME}?signature=secret" in [
        url for url, _ in network.calls
    ]
    [(npm, npm_environment)] = [
        (command, env) for command, env in commands.run if "ci" in command
    ]
    assert npm == [
        str(job / "npm"),
        "ci",
        "--prefix",
        str(tools),
        "--registry",
        "https://registry.npmjs.org/",
        "--replace-registry-host",
        "never",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]
    assert npm_environment is not None
    assert npm_environment["PATH"] == f"{job}:{job}"
    assert {
        name: value
        for name, value in npm_environment.items()
        if name.lower().startswith("npm_config_")
    } == {
        "npm_config_userconfig": str(home / "npm-config" / "user"),
        "npm_config_globalconfig": str(home / "npm-config" / "global"),
        "npm_config_cache": str(home / "npm"),
        "npm_config_update_notifier": "false",
    }
    assert (home / "npm-config" / "user").read_text() == ""
    assert (home / "npm-config" / "global").read_text() == ""
    assert "ACTIONS_ID_TOKEN_REQUEST_URL" not in npm_environment
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in npm_environment
    locked = (given.action / "release" / "typescript-runtime-lock.json").read_text()
    assert (tools / "package-lock.json").read_text() == locked
    assert json.loads((tools / "package.json").read_text()) == {
        "name": "invarith-typescript-runtime-dependencies",
        "private": True,
        "dependencies": {"pinned-npm-dep": "1.0.0"},
    }
    runtime = tools / "node_modules" / "@invarith" / "runtime"
    assert sorted(p.relative_to(runtime).as_posix() for p in runtime.rglob("*")) == [
        "dist",
        "dist/index.js",
        "package.json",
    ]
    check, _ = commands.run[-1]
    assert check[check.index("--python") : check.index("--minutes")] == [
        "--python",
        sys.executable,
        "--tools",
        str(home / "tools"),
        "--node",
        str(job / "node"),
        "--typescript-tools",
        str(tools),
    ]


def test_a_project_without_a_package_json(tmp_path: Path, given: Release) -> None:
    """It isn't a TypeScript project: the release's TypeScript runtime isn't
    downloaded or installed, and it needs no Node.js."""
    given = with_typescript(given, tmp_path)
    network, commands = Network(given), Commands()
    code, said = bootstrapped(environment(tmp_path, given), network, commands)
    assert code == 0, said
    assert not any(TYPESCRIPT_RUNTIME in url for url, _ in network.calls)
    assert not any("ci" in command for command, _ in commands.run)
    assert "--node" not in commands.run[-1][0]


def test_a_release_without_the_typescript_runtime(
    tmp_path: Path, given: Release
) -> None:
    """A release made before TypeScript support, such as 0.7.0, installs nothing for
    TypeScript, even for a project with a package.json, and needs no Node.js."""
    variables = environment(
        tmp_path, given, INVARITH_PROJECT=str(typescript_project(tmp_path))
    )
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    assert "--node" not in commands.run[-1][0]


def test_a_typescript_project_s_node_input(tmp_path: Path, given: Release) -> None:
    """A path, made absolute, without following links: a link to Node.js stays the
    link. npm is looked for beside the path given, then on the job's PATH."""
    given = with_typescript(given, tmp_path)
    elsewhere = tmp_path / "node-26" / "bin"
    elsewhere.mkdir(parents=True)
    for name in ("node", "npm"):
        (elsewhere / name).write_text("#!/bin/sh\n")
        (elsewhere / name).chmod(0o755)
    linked = tmp_path / "node-link"
    linked.symlink_to(elsewhere / "node")
    variables = typescript_job(tmp_path, given, INVARITH_NODE=str(linked))
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    check = commands.run[-1][0]
    assert check[check.index("--node") + 1] == str(linked)
    [npm] = [command for command, _ in commands.run if "ci" in command]
    assert npm[0] == str(tmp_path / "job-bin" / "npm")
    assert npm[0] != str(elsewhere / "npm")


def test_npm_beside_the_project_s_node(tmp_path: Path, given: Release) -> None:
    given = with_typescript(given, tmp_path)
    elsewhere = tmp_path / "node-26" / "bin"
    elsewhere.mkdir(parents=True)
    for name in ("node", "npm"):
        (elsewhere / name).write_text("#!/bin/sh\n")
        (elsewhere / name).chmod(0o755)
    variables = typescript_job(tmp_path, given, INVARITH_NODE=str(elsewhere / "node"))
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    [(npm, npm_environment)] = [
        (command, env) for command, env in commands.run if "ci" in command
    ]
    assert npm[0] == str(elsewhere / "npm")
    assert npm_environment is not None
    assert npm_environment["PATH"].startswith(f"{elsewhere}:")


def test_a_typescript_project_without_node(tmp_path: Path, given: Release) -> None:
    """It stops before asking for a token, saying how to set up Node.js."""
    given = with_typescript(given, tmp_path)
    variables = typescript_job(tmp_path, given, INVARITH_NODE="node-99")
    network = Network(given)
    code, said = bootstrapped(variables, network, Commands())
    assert code == 2
    assert "node-99 wasn't found" in said
    assert "actions/setup-node" in said
    assert network.calls == []


def test_a_typescript_project_without_npm(tmp_path: Path, given: Release) -> None:
    """It stops before asking for a token, as without Node.js."""
    given = with_typescript(given, tmp_path)
    variables = typescript_job(
        tmp_path, given, INVARITH_JOB_PATH=str(job_bin(tmp_path / "x", "node"))
    )
    network = Network(given)
    code, said = bootstrapped(variables, network, Commands())
    assert code == 2
    assert "npm wasn't found beside the project's Node.js" in said
    assert network.calls == []


def test_a_typescript_project_with_neither_node_nor_python(
    tmp_path: Path, given: Release
) -> None:
    """It says only that Node.js wasn't found, not that it would check the project
    without Python."""
    given = with_typescript(given, tmp_path)
    variables = typescript_job(
        tmp_path,
        given,
        INVARITH_PYTHON="python",
        INVARITH_JOB_PATH=str(job_bin(tmp_path / "x")),
    )
    code, said = bootstrapped(variables, Network(given), Commands())
    assert code == 2
    assert said.startswith(
        "::error title=invarith::The project is a TypeScript project"
    )
    assert "::notice" not in said


def test_a_typescript_project_needs_no_python(tmp_path: Path, given: Release) -> None:
    """With the python input at its default, and no python found, Python's runtime
    isn't installed, a notice says so, and the check isn't given an interpreter."""
    given = with_typescript(given, tmp_path)
    variables = typescript_job(tmp_path, given, INVARITH_PYTHON="python")
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    assert said.startswith("::notice title=invarith::No python was found")
    assert not any("--target" in command for command, _ in commands.run)
    check = commands.run[-1][0]
    assert "--python" not in check
    assert "--tools" not in check
    assert "--typescript-tools" in check


@pytest.mark.parametrize(
    ("project", "python"),
    [("typescript", "python3.99"), ("python", "python")],
    ids=["set for a TypeScript project", "the default for a Python project"],
)
def test_an_interpreter_the_project_needs(
    tmp_path: Path, given: Release, project: str, python: str
) -> None:
    """One the workflow sets must be found, as must the default for a project that
    isn't a TypeScript project."""
    given = with_typescript(given, tmp_path)
    variables = typescript_job(tmp_path, given, INVARITH_PYTHON=python)
    if project == "python":
        variables["INVARITH_PROJECT"] = str(tmp_path)
    network = Network(given)
    code, said = bootstrapped(variables, network, Commands())
    assert code == 2
    assert f"The project's interpreter, {python}, wasn't found" in said
    assert network.calls == []


def test_a_typescript_project_s_python_that_can_t_take_the_runtime(
    tmp_path: Path, given: Release
) -> None:
    """With the python input at its default, a python uv can't install the runtime
    for, such as one older than it supports, is like none found: a notice says so,
    and the check isn't given an interpreter."""
    given = with_typescript(given, tmp_path)
    variables = typescript_job(
        tmp_path,
        given,
        INVARITH_PYTHON="python",
        INVARITH_JOB_PATH=str(job_bin(tmp_path, "node", "npm", "python")),
    )
    commands = Commands(exits={"--target": 1})
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    assert said.startswith(
        "::notice title=invarith::invarith's Python runtime couldn't be installed for "
        f"{tmp_path / 'job-bin' / 'python'}"
    )
    check = commands.run[-1][0]
    assert "--python" not in check
    assert "--tools" not in check
    assert "--typescript-tools" in check


@pytest.mark.parametrize(
    "project", ["python", "typescript"], ids=["a Python project", "python set"]
)
def test_a_python_that_can_t_take_the_runtime(
    tmp_path: Path, given: Release, project: str
) -> None:
    """For a project that isn't a TypeScript project, or a python the workflow sets,
    it stops."""
    given = with_typescript(given, tmp_path)
    variables = typescript_job(tmp_path, given)
    if project == "python":
        variables["INVARITH_PROJECT"] = str(tmp_path)
    commands = Commands(exits={"--target": 1})
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 2
    assert "uv couldn't install the Python runtime" in said
    assert not any("check" in command for command, _ in commands.run)


def test_npm_fails(tmp_path: Path, given: Release) -> None:
    given = with_typescript(given, tmp_path)
    commands = Commands(exits={"ci": 1})
    code, said = bootstrapped(typescript_job(tmp_path, given), Network(given), commands)
    assert code == 2
    assert "npm couldn't install the TypeScript runtime's pinned packages" in said
    assert not any("check" in command for command, _ in commands.run)


@pytest.mark.parametrize(
    "broken",
    ["two packages", "a short checksum", "another name", "no lockfile"],
)
def test_a_release_s_typescript_runtime_that_isn_t_whole(
    tmp_path: Path, given: Release, broken: str
) -> None:
    given = with_typescript(given, tmp_path)
    directory = given.action / "release"
    named = json.loads((directory / "invarith.json").read_text())
    if broken == "two packages":
        named["typescript_runtime"]["invarith-typescript-runtime-0.5.0.tgz"] = "a" * 64
    elif broken == "a short checksum":
        named["typescript_runtime"][TYPESCRIPT_RUNTIME] = "abc"
    elif broken == "another name":
        named["typescript_runtime"] = {"left-pad-1.3.0.tgz": "a" * 64}
    else:
        (directory / "typescript-runtime-lock.json").unlink()
    (directory / "invarith.json").write_text(json.dumps(named))
    code, said = bootstrapped(environment(tmp_path, given), Network(given), Commands())
    assert code == 2
    assert "::error title=invarith::release/" in said


def test_a_lockfile_without_its_root_package(tmp_path: Path, given: Release) -> None:
    """It's found with the rest of the release, before asking for a token."""
    given = with_typescript(given, tmp_path)
    lock = given.action / "release" / "typescript-runtime-lock.json"
    lock.write_text(json.dumps({"lockfileVersion": 3, "packages": {}}))
    network = Network(given)
    code, said = bootstrapped(typescript_job(tmp_path, given), network, Commands())
    assert code == 2
    assert "isn't an npm lockfile whose root package names its dependencies" in said
    assert network.calls == []


def _with_member(path: Path, member: tarfile.TarInfo, data: bytes = b"") -> None:
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"{}"))
        archive.addfile(member, io.BytesIO(data) if member.isfile() else None)


@pytest.mark.parametrize(
    ("member", "said"),
    [
        ("link", "which isn't a file or a directory"),
        ("outside", "outside package/"),
        ("parent", "couldn't be unpacked"),
    ],
)
def test_an_npm_package_that_can_t_be_unpacked(
    tmp_path: Path, member: str, said: str
) -> None:
    """Only its files and directories, under package/, and only into its
    directory."""
    package = tmp_path / TYPESCRIPT_RUNTIME
    if member == "link":
        info = tarfile.TarInfo("package/dist")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc"
        _with_member(package, info)
    else:
        name = "dist/index.js" if member == "outside" else "package/../../escaped.js"
        info = tarfile.TarInfo(name)
        info.size = 1
        _with_member(package, info, b"x")
    into = tmp_path / "tools" / "node_modules" / "@invarith" / "runtime"
    with pytest.raises(bootstrap.Stop, match=said):
        bootstrap.unpack(package, into)
    assert not list(tmp_path.rglob("escaped.js"))


# A Java project


def java_job(
    tmp_path: Path, given: Release, build: str = "gradle", **changes: str
) -> dict[str, str]:
    """A push's job for a Java project, built with Gradle or Maven, with a JDK's java
    and javac on its PATH."""
    found = environment(
        tmp_path,
        given,
        INVARITH_PROJECT=str(java_project(tmp_path, build)),
        INVARITH_JOB_PATH=str(job_bin(tmp_path, "java", "javac")),
    )
    found.update(changes)
    return found


@pytest.mark.parametrize("build", ["gradle", "maven"])
def test_a_java_project(tmp_path: Path, given: Release, build: str) -> None:
    """For a project with a Gradle or Maven build file, the Java tools' jars are
    downloaded and checked with the wheels, and put in a directory of their own as
    `runtime.jar` and `maven-extension.jar`. Once the runtimes are installed, the
    project's own build gives its classpath where it is, through `invarith setup
    --in-place`, on the job's PATH, without the job's means of asking for OIDC tokens;
    then the check is given the project's java, the Java tools and the classpath."""
    given = with_java(given, tmp_path)
    variables = java_job(tmp_path, given, build)
    network, commands = Network(given), Commands()
    code, said = bootstrapped(variables, network, commands)
    assert code == 0, said
    downloaded = {url.split("?")[0] for url, _ in network.calls}
    for jar in (RUNTIME_JAR, MAVEN_EXTENSION_JAR):
        assert f"https://storage.example/{jar}" in downloaded
    home = tmp_path / "runner" / "invarith"
    project = tmp_path / "shop"
    java_tools = home / "java-tools"
    assert {p.name: p.read_bytes() for p in java_tools.iterdir()} == {
        "runtime.jar": given.java[RUNTIME_JAR].read_bytes(),
        "maven-extension.jar": given.java[MAVEN_EXTENSION_JAR].read_bytes(),
    }
    cli = str(home / "cli" / "bin" / "invarith")
    (setup, setup_environment), (check, _) = commands.run[-2:]
    assert setup == [
        cli,
        "setup",
        "--language",
        "java",
        "--in-place",
        "--project",
        str(project),
        "--into",
        str(home / "java-setup"),
        "--java-tools",
        str(java_tools),
    ]
    assert setup_environment is not None
    assert setup_environment["PATH"] == str(tmp_path / "job-bin")
    assert "ACTIONS_ID_TOKEN_REQUEST_URL" not in setup_environment
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in setup_environment
    assert setup_environment["GITHUB_EVENT_NAME"] == "push"  # the job's, otherwise
    # The minutes input bounds the build, as it bounds the check; nothing else is.
    assert commands.timeouts[-2] == 15 * 60
    assert set(commands.timeouts[:-2] + commands.timeouts[-1:]) == {None}
    assert check == [
        cli,
        "--server",
        SERVER,
        "check",
        "--project",
        str(project),
        "--python",
        sys.executable,
        "--tools",
        str(home / "tools"),
        "--java",
        str(tmp_path / "job-bin" / "java"),
        "--java-tools",
        str(java_tools),
        "--java-classpath",
        str(home / "java-setup" / "java" / "classpath.json"),
        "--minutes",
        "15",
    ]


def test_a_project_without_a_java_build(tmp_path: Path, given: Release) -> None:
    """Without one of Java's markers in the project's directory, as in one holding
    only Java sources, nothing is downloaded or installed for Java, and no JDK is
    needed."""
    given = with_java(given, tmp_path)
    project = java_project(tmp_path, "gradle")
    for name in ("settings.gradle.kts", "build.gradle.kts"):
        (project / name).unlink()
    variables = environment(tmp_path, given, INVARITH_PROJECT=str(project))
    network, commands = Network(given), Commands()
    code, said = bootstrapped(variables, network, commands)
    assert code == 0, said
    assert not any(".jar" in url for url, _ in network.calls)
    assert not any("setup" in command for command, _ in commands.run)
    assert "--java" not in commands.run[-1][0]


def test_the_markers_are_the_release_s(tmp_path: Path, given: Release) -> None:
    """A project needs the Java tools for a file the release names as one of Java's
    markers, whatever it is, and not for a build file it doesn't name."""
    given = with_java(given, tmp_path, {"java": ["build.sbt"]})
    variables = java_job(tmp_path, given, "maven")
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    assert "--java" not in commands.run[-1][0]
    (tmp_path / "shop" / "build.sbt").write_text("")
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    assert "--java" in commands.run[-1][0]


@pytest.mark.parametrize(
    ("markers", "problem"),
    [
        ({}, "holds the Java runtime, but names no"),
        (
            {"java": JAVA_MARKERS, "typescript": ["package.json"]},
            "names markers for typescript, but holds no runtime for it",
        ),
        ({"java": []}, "as a list of the names of files"),
        ({"java": "pom.xml"}, "as a list of the names of files"),
        ({"java": ["../pom.xml"]}, "as a list of the names of files"),
        ({"java": ["app/pom.xml"]}, "as a list of the names of files"),
        ({"java": [""]}, "as a list of the names of files"),
        (None, "holds the Java runtime, but names no markers"),
    ],
    ids=[
        "none for Java",
        "for a runtime it doesn't hold",
        "empty",
        "not a list",
        "up",
        "down",
        "blank",
        "no markers",
    ],
)
def test_a_release_s_markers_that_can_t_be_used(
    tmp_path: Path,
    given: Release,
    markers: dict[str, object] | None,
    problem: str,
) -> None:
    """Each runtime the release holds needs its markers, each a file's name, which is
    looked for in the project's directory itself; and each language with markers
    needs its runtime, since a runtime dropped from a release that still names its
    markers would leave its projects without it, and without a word."""
    given = with_java(given, tmp_path)
    named_path = given.action / "release" / "invarith.json"
    named = json.loads(named_path.read_text())
    if markers is None:
        del named["markers"]
    else:
        named["markers"] = markers
    named_path.write_text(json.dumps(named))
    code, said = bootstrapped(java_job(tmp_path, given), Network(given), Commands())
    assert code == 2
    assert problem in said


@pytest.mark.parametrize(
    "change",
    ["one jar", "another jar too", "two runtime jars", "a short checksum", "a path"],
)
def test_a_release_s_java_runtime_that_isn_t_whole(
    tmp_path: Path, given: Release, change: str
) -> None:
    given = with_java(given, tmp_path)
    named_path = given.action / "release" / "invarith.json"
    named = json.loads(named_path.read_text())
    jars = named["java_runtime"]
    if change == "one jar":
        del jars[MAVEN_EXTENSION_JAR]
    elif change == "another jar too":
        jars["invarith-other-0.6.0.jar"] = "1" * 64
    elif change == "a short checksum":
        jars[RUNTIME_JAR] = "1" * 63
    elif change == "two runtime jars":
        del jars[MAVEN_EXTENSION_JAR]
        jars[f"invarith-runtime-{VERSION}-all.jar"] = "1" * 64
    else:
        jars[f"invarith-runtime-{VERSION}/x.jar"] = jars.pop(RUNTIME_JAR)
    named_path.write_text(json.dumps(named))
    code, said = bootstrapped(java_job(tmp_path, given), Network(given), Commands())
    assert code == 2
    assert "needs the Java tools' jars" in said


def test_a_java_project_s_java_input(tmp_path: Path, given: Release) -> None:
    """The java input, a path, is made absolute, and runs the harnesses, with the
    javac beside it."""
    given = with_java(given, tmp_path)
    jdk = job_bin(tmp_path / "jdk-21", "java", "javac")
    variables = java_job(tmp_path, given, INVARITH_JAVA=os.path.relpath(jdk / "java"))
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    check = commands.run[-1][0]
    assert check[check.index("--java") + 1] == str(jdk / "java")


def test_a_java_project_without_java(tmp_path: Path, given: Release) -> None:
    """With the java input at its default and no java found, the project's build files
    needn't mean its claims are bound in Java: a warning says so, the Java tools aren't
    downloaded or installed, and the check isn't given them. A java input the workflow
    sets must be found."""
    given = with_java(given, tmp_path)
    empty = job_bin(tmp_path / "empty")
    variables = java_job(tmp_path, given, INVARITH_JOB_PATH=str(empty))
    network, commands = Network(given), Commands()
    code, said = bootstrapped(variables, network, commands)
    assert code == 0, said
    assert said.startswith(
        "::warning title=invarith::The project has a Java build, but java wasn't "
        "found, so invarith checks it without its Java tools"
    )
    assert not any(".jar" in url for url, _ in network.calls)
    assert not any("setup" in command for command, _ in commands.run)
    assert "--java" not in commands.run[-1][0]
    variables["INVARITH_JAVA"] = "java-21"
    code, said = bootstrapped(variables, Network(given), Commands())
    assert code == 2
    assert "The project's JDK, the java input, is java-21, but java-21 wasn't" in said


def test_a_jre_s_java_isn_t_a_jdk_s(tmp_path: Path, given: Release) -> None:
    """A java with no javac beside it can't compile the harnesses, though a javac is
    elsewhere on the PATH, which may be another JDK's, as when `/usr/bin/java` and
    `/usr/bin/javac` are links to two JDKs: each link is followed. At the java
    input's default, a warning says so, and the Java tools aren't installed; a java
    input the workflow sets must be a JDK's."""
    given = with_java(given, tmp_path)
    jre = job_bin(tmp_path / "jre", "java")
    jdk = job_bin(tmp_path / "jdk", "javac")
    links = tmp_path / "usr-bin"
    links.mkdir()
    (links / "java").symlink_to(jre / "java")
    (links / "javac").symlink_to(jdk / "javac")
    variables = java_job(tmp_path, given, INVARITH_JOB_PATH=str(links))
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    assert f"but there's no javac beside {jre / 'java'}, so it isn't a JDK's" in said
    assert "--java" not in commands.run[-1][0]
    variables["INVARITH_JAVA"] = str(links / "java")
    code, said = bootstrapped(variables, Network(given), Commands())
    assert code == 2
    assert f"there's no javac beside {jre / 'java'}" in said


def test_a_java_by_a_link_runs_as_the_jdk_s(tmp_path: Path, given: Release) -> None:
    """A java found by a link, such as `/usr/bin/java`, is given to the check as the
    JDK's own, which the link leads to, so its javac is the JDK's too."""
    given = with_java(given, tmp_path)
    jdk = job_bin(tmp_path / "jdk-21", "java", "javac")
    links = tmp_path / "usr-bin"
    links.mkdir()
    (links / "java").symlink_to(jdk / "java")
    variables = java_job(tmp_path, given, INVARITH_JOB_PATH=str(links))
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    check = commands.run[-1][0]
    assert check[check.index("--java") + 1] == str(jdk / "java")


def test_a_java_project_needs_no_python(tmp_path: Path, given: Release) -> None:
    """With the python input at its default, and no python found, Python's runtime
    isn't installed, a notice says so, and the check isn't given an interpreter."""
    given = with_java(given, tmp_path)
    variables = java_job(tmp_path, given, INVARITH_PYTHON="python")
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    assert said.startswith(
        "::notice title=invarith::No python was found, so invarith checks this Java "
        "project without its Python runtime"
    )
    assert not any("--target" in command for command, _ in commands.run)
    check = commands.run[-1][0]
    assert "--python" not in check
    assert "--java-tools" in check


def test_a_java_project_s_python_that_can_t_take_the_runtime(
    tmp_path: Path, given: Release
) -> None:
    """With the python input at its default, a python uv can't install the runtime for
    is like none found, for a Java project as for a TypeScript one."""
    given = with_java(given, tmp_path)
    variables = java_job(
        tmp_path,
        given,
        INVARITH_PYTHON="python",
        INVARITH_JOB_PATH=str(job_bin(tmp_path, "java", "javac", "python")),
    )
    commands = Commands(exits={"--target": 1})
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    assert said.startswith(
        "::notice title=invarith::invarith's Python runtime couldn't be installed for "
        f"{tmp_path / 'job-bin' / 'python'}, so invarith checks this Java project"
    )
    check = commands.run[-1][0]
    assert "--python" not in check
    assert "--java-tools" in check


def test_a_project_with_both_a_package_json_and_a_java_build(
    tmp_path: Path, given: Release
) -> None:
    """It needs both runtimes, each installed, and the check is given both."""
    given = with_java(with_typescript(given, tmp_path), tmp_path)
    variables = java_job(
        tmp_path,
        given,
        INVARITH_JOB_PATH=str(job_bin(tmp_path, "java", "javac", "node", "npm")),
        INVARITH_PYTHON="python",
    )
    (tmp_path / "shop" / "package.json").write_text('{"type": "module"}\n')
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    assert "checks this TypeScript and Java project" in said
    check = commands.run[-1][0]
    assert "--typescript-tools" in check
    assert "--java-classpath" in check


def test_a_java_build_that_runs_past_its_time(tmp_path: Path, given: Release) -> None:
    """A build still running after the minutes input's time is stopped, and the step
    goes on, as for a build that can't give its classpath, with a warning that says
    so."""
    given = with_java(given, tmp_path)
    variables = java_job(tmp_path, given, INVARITH_MINUTES="2.5")
    commands = Commands(exits={"setup": bootstrap.TIMED_OUT})
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 0, said
    assert commands.timeouts[-2] == 150
    assert (
        "::warning title=invarith::The project's Java build didn't give its classpath "
        "within 2.5 minutes, the minutes input, so it was stopped." in said
    )
    check = commands.run[-1][0]
    assert "--java-tools" in check
    assert "--java-classpath" not in check


def test_a_command_is_stopped_at_its_time_limit() -> None:
    """`run` gives TIMED_OUT for a command still running at its time limit, which it
    stops, and a command's own exit code otherwise."""
    assert bootstrap.run(["sleep", "5"], None, timeout=0.2) == bootstrap.TIMED_OUT
    assert bootstrap.run(["sh", "-c", "exit 3"], None) == 3


def test_a_java_input_set_to_java_must_be_a_jdk_s(
    tmp_path: Path, given: Release
) -> None:
    """action.yml gives the java input as empty when the workflow doesn't set it, so
    one the workflow sets to `java`, its default's name, must be a JDK's, as any other
    it sets must, and isn't taken as left at its default."""
    given = with_java(given, tmp_path)
    empty = job_bin(tmp_path / "empty")
    variables = java_job(tmp_path, given, INVARITH_JOB_PATH=str(empty))
    for left in ({}, {"INVARITH_JAVA": ""}):
        code, said = bootstrapped(variables | left, Network(given), Commands())
        assert code == 0, said
        assert "::warning title=invarith::The project has a Java build" in said
    variables["INVARITH_JAVA"] = "java"
    code, said = bootstrapped(variables, Network(given), Commands())
    assert code == 2
    assert "The project's JDK, the java input, is java, but java wasn't found" in said


def test_a_java_project_without_a_jdk_or_python(tmp_path: Path, given: Release) -> None:
    """Without a JDK, a Java project is checked as one without a Java build, which
    needs an interpreter: with neither, the step stops, saying no JDK was found, not
    only the interpreter, and without first warning that it goes on."""
    given = with_java(given, tmp_path)
    empty = job_bin(tmp_path / "empty")
    variables = java_job(
        tmp_path, given, INVARITH_JOB_PATH=str(empty), INVARITH_PYTHON="python"
    )
    commands = Commands()
    code, said = bootstrapped(variables, Network(given), commands)
    assert code == 2
    assert said == (
        "::error title=invarith::The project has a Java build, but java wasn't "
        "found, and no python was found either, so invarith has nothing to check it "
        "on. Set up a JDK before the invarith step, such as with actions/setup-java, "
        "or set the java input to the project's JDK's java.\n"
    )
    assert commands.run == []


def test_a_java_build_that_can_t_give_its_classpath(
    tmp_path: Path, given: Release
) -> None:
    """The step goes on without the classpath, since the project's build files needn't
    mean its claims are bound in Java: the check is given the Java tools, so each claim
    bound in Java is unknown, saying why, and a warning says the build failed."""
    given = with_java(given, tmp_path)
    commands = Commands(exits={"setup": 1})
    code, said = bootstrapped(java_job(tmp_path, given), Network(given), commands)
    assert code == 0, said
    assert "::warning title=invarith::The project's Java build couldn't give" in said
    check = commands.run[-1][0]
    assert "check" in check
    assert "--java-tools" in check
    assert "--java-classpath" not in check


# For real: uv, and a stand-in for the Run API on this machine


class Served(BaseHTTPRequestHandler):
    """GitHub's token service and invarith's server, over HTTP on this machine."""

    release: Release

    def do_GET(self) -> None:
        port = cast(tuple[str, int], self.server.server_address)[1]
        if self.path.startswith("/token?"):
            assert self.path.endswith("audience=https%3A%2F%2Finvarith.ai")
            self.answer(200, json.dumps({"value": OIDC}).encode())
        elif self.path == f"/v1/releases/{VERSION}":
            assert self.headers["Authorization"] == f"Bearer {OIDC}"
            files = [
                {
                    "name": name,
                    "sha256": sha256(path),
                    "size": path.stat().st_size,
                    "url": f"http://127.0.0.1:{port}/files/{name}",
                }
                for name, path in self.release.offered.items()
            ]
            self.answer(200, json.dumps({"version": VERSION, "files": files}).encode())
        elif self.path.startswith("/files/"):
            self.answer(
                200,
                self.release.offered[self.path.removeprefix("/files/")].read_bytes(),
            )
        else:
            self.answer(404, b"{}")

    def answer(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def served_release(given: Release) -> Generator[str]:
    """The stand-in for GitHub's token service and invarith's server, serving the
    release, at the address it gives."""
    handler = type("Handler", (Served,), {"release": given})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.fixture
def served(given: Release) -> Iterator[str]:
    with served_release(given) as address:
        yield address


def for_real(tmp_path: Path, given: Release, address: str) -> dict[str, str]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv isn't on the PATH")
    return environment(
        tmp_path,
        given,
        ACTIONS_ID_TOKEN_REQUEST_URL=f"{address}/token?run=1",
        INVARITH_SERVER=address,
        INVARITH_JOB_PATH=os.environ["PATH"],
        PATH=os.environ["PATH"],
        UV=uv,
        FAKE_RECORD=str(tmp_path / "record.json"),
        FAKE_EXIT="1",
        HOME=os.environ.get("HOME", str(tmp_path)),
    )


def test_the_tools_installed_and_checked(
    tmp_path: Path, given: Release, served: str
) -> None:
    """The packages downloaded and checked, installed beside the project's
    environment with their pinned packages, and the check run, whose exit code is
    the step's."""
    variables = for_real(tmp_path, given, served)
    out = io.StringIO()
    code = bootstrap.main(variables, out=out)
    assert code == 1, out.getvalue()
    home = tmp_path / "runner" / "invarith"
    recorded = json.loads((tmp_path / "record.json").read_text())
    assert recorded["argv"] == [
        "--server",
        served,
        "check",
        "--project",
        str(tmp_path),
        "--python",
        sys.executable,
        "--tools",
        str(home / "tools"),
        "--minutes",
        "15",
    ]
    assert recorded["path"] == variables["INVARITH_JOB_PATH"]
    assert {p.name for p in (home / "tools").iterdir()} >= {
        "invarith_runtime",
        "pinned_dep",
    }
    site = next((home / "cli" / "lib").glob("python*/site-packages"))
    assert (site / "pinned_dep").is_dir()
    assert (site / "invarith_core").is_dir()


def test_a_pinned_package_with_another_hash_isn_t_installed(
    tmp_path: Path, given: Release, served: str
) -> None:
    """Nothing is installed but at the hash the release pins."""
    pins = given.action / "release" / "cli.txt"
    pins.write_text(
        "\n".join(
            line if "--hash" not in line else "    --hash=sha256:" + "0" * 64
            for line in pins.read_text().splitlines()
        )
        + "\n"
    )
    out = io.StringIO()
    code = bootstrap.main(for_real(tmp_path, given, served), out=out)
    assert code == 2
    assert "uv couldn't install the CLI's pinned packages" in out.getvalue()


def typescript_for_real(tmp_path: Path, given: Release, address: str) -> dict[str, str]:
    node, npm = shutil.which("node"), shutil.which("npm")
    if node is None or npm is None:
        pytest.skip("node and npm aren't on the PATH")
    variables = for_real(tmp_path, given, address)
    variables["INVARITH_PROJECT"] = str(typescript_project(tmp_path))
    return variables


def test_the_typescript_runtime_installed_and_checked(
    tmp_path: Path, given: Release
) -> None:
    """npm installs the pinned package from the lockfile, running none of its install
    scripts, and reading none of the job's npm configuration, the runtime is
    unpacked beside it, where Node.js imports it from, and the check is given
    both."""
    given = with_typescript(given, tmp_path)
    npmrc = tmp_path / "job.npmrc"
    npmrc.write_text("dry-run=true\n")
    with served_release(given) as address:
        variables = typescript_for_real(tmp_path, given, address)
        # The job's npm configuration, which would install nothing, isn't read.
        variables |= {"NPM_CONFIG_USERCONFIG": str(npmrc), "npm_config_dry_run": "true"}
        out = io.StringIO()
        code = bootstrap.main(variables, out=out)
    assert code == 1, out.getvalue()
    tools = tmp_path / "runner" / "invarith" / "typescript"
    recorded = json.loads((tmp_path / "record.json").read_text())
    node = shutil.which("node")
    assert recorded["argv"][-6:] == [
        "--node",
        node,
        "--typescript-tools",
        str(tools),
        "--minutes",
        "15",
    ]
    assert (tools / "node_modules" / "pinned-npm-dep" / "index.js").is_file()
    assert not list(tools.rglob("install-script-ran"))
    imported = subprocess.run(
        [
            str(node),
            "--input-type=module",
            "--eval",
            'import { VERSION } from "@invarith/runtime"; console.log(VERSION);',
        ],
        cwd=tools,
        capture_output=True,
        text=True,
        check=True,
    )
    assert imported.stdout.strip() == VERSION


def test_a_pinned_npm_package_with_another_integrity_isn_t_installed(
    tmp_path: Path, given: Release
) -> None:
    given = with_typescript(given, tmp_path)
    lock_path = given.action / "release" / "typescript-runtime-lock.json"
    lock = json.loads(lock_path.read_text())
    lock["packages"]["node_modules/pinned-npm-dep"]["integrity"] = (
        "sha512-" + base64.b64encode(b"0" * 64).decode()
    )
    lock_path.write_text(json.dumps(lock))
    with served_release(given) as address:
        out = io.StringIO()
        code = bootstrap.main(typescript_for_real(tmp_path, given, address), out=out)
    assert code == 2
    assert "npm couldn't install the TypeScript runtime's pinned packages" in (
        out.getvalue()
    )


def test_the_java_tools_installed_and_checked(tmp_path: Path, given: Release) -> None:
    """For a small Java project, the jars downloaded from the stand-in for the Run API
    and checked, installed beside the project's environment, then the CLI's setup run
    in place, without the job's means of asking for OIDC tokens, and the check run
    with the classpath it recorded. Which build the project has changes none of it,
    as test_a_java_project shows."""
    given = with_java(given, tmp_path)
    with served_release(given) as address:
        variables = for_real(tmp_path, given, address)
        project = java_project(tmp_path, "gradle")
        jdk = job_bin(tmp_path / "jdk", "java", "javac")
        variables["INVARITH_PROJECT"] = str(project)
        variables["INVARITH_JAVA"] = str(jdk / "java")
        out = io.StringIO()
        code = bootstrap.main(variables, out=out)
    assert code == 1, out.getvalue()
    home = tmp_path / "runner" / "invarith"
    java_tools = home / "java-tools"
    assert (java_tools / "runtime.jar").read_bytes() == (
        given.java[RUNTIME_JAR].read_bytes()
    )
    assert (java_tools / "maven-extension.jar").read_bytes() == (
        given.java[MAVEN_EXTENSION_JAR].read_bytes()
    )
    setup = json.loads((tmp_path / "record.json.setup").read_text())
    assert setup["argv"] == [
        "setup",
        "--language",
        "java",
        "--in-place",
        "--project",
        str(project),
        "--into",
        str(home / "java-setup"),
        "--java-tools",
        str(java_tools),
    ]
    assert setup["path"] == variables["INVARITH_JOB_PATH"]
    assert setup["token"] is False
    check = json.loads((tmp_path / "record.json").read_text())
    assert check["argv"][check["argv"].index("--java") :] == [
        "--java",
        str(jdk / "java"),
        "--java-tools",
        str(java_tools),
        "--java-classpath",
        str(home / "java-setup" / "java" / "classpath.json"),
        "--minutes",
        "15",
    ]
    assert check["token"] is True  # the check starts the run with it


def test_the_audience() -> None:
    """The Run API accepts the job's token for this audience only."""
    assert AUDIENCE == "https://invarith.ai"
