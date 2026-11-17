"""bootstrap.py: which events it verifies, the release it names, the job's token, the
Run API's links, the downloads and their checks, the installs and `invarith check`.

Most tests stand in for the network and for the commands it runs. The last ones run
it for real, with uv, against a stand-in for the Run API on this machine that serves
small wheels built here, so they need no package index."""

import base64
import hashlib
import io
import json
import os
import shutil
import sys
import threading
import zipfile
from collections.abc import Iterator, Mapping, Sequence
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


# The CLI stands in for invarith's: it records how it was run, and exits as asked.
FAKE_CLI = """import json, os, sys

def main():
    with open(os.environ["FAKE_RECORD"], "w") as out:
        json.dump({"argv": sys.argv[1:], "path": os.environ["PATH"]}, out)
    return int(os.environ.get("FAKE_EXIT", "0"))
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pinned(wheel_path: Path) -> str:
    """A pinned requirement on a wheel on this machine, in requirements format."""
    name = wheel_path.name.split("-")[0].replace("_", "-")
    return (
        f"{name} @ {wheel_path.resolve().as_uri()} \\\n"
        f"    --hash=sha256:{sha256(wheel_path)}\n"
    )


@dataclass
class Release:
    """An action release, with the packages it names, built here."""

    action: Path
    packages: dict[str, Path]


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
                for name, path in self.release.packages.items()
            ]
            return Answer(
                200, json.dumps({"version": VERSION, "files": files}).encode()
            )
        prefix = "https://storage.example/"
        if url.startswith(prefix):
            name = url.removeprefix(prefix).split("?")[0]
            return Answer(200, self.release.packages[name].read_bytes())
        return Answer(404, b"{}")


@dataclass
class Commands:
    """The commands it runs, each answered with `exits`'s code, or 0."""

    run: list[tuple[list[str], Mapping[str, str] | None]] = field(
        default_factory=list[Any]
    )
    exits: dict[str, int] = field(default_factory=dict[str, int])

    def __call__(
        self, command: Sequence[str], environment: Mapping[str, str] | None
    ) -> int:
        self.run.append((list(command), environment))
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
                for name, path in self.release.packages.items()
            ]
            self.answer(200, json.dumps({"version": VERSION, "files": files}).encode())
        elif self.path.startswith("/files/"):
            self.answer(
                200,
                self.release.packages[self.path.removeprefix("/files/")].read_bytes(),
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


@pytest.fixture
def served(given: Release) -> Iterator[str]:
    handler = type("Handler", (Served,), {"release": given})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


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


def test_the_audience() -> None:
    """The Run API accepts the job's token for this audience only."""
    assert AUDIENCE == "https://invarith.ai"
