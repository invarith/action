# invarith/action

invarith's CI step. It verifies the claims in a repository's `INVARITH.md` against the code, with invarith's hosted service, and invarith's GitHub App reports the results as a check run on the pull request or commit.

This repository holds only a small bootstrapper. invarith's tools, the CLI, the verifier and the runtimes, are private: the action downloads them from invarith with the job's OIDC token, and only repositories with invarith's GitHub App installed get them.

## Setup

Add a workflow, such as `.github/workflows/invarith.yml`:

```yaml
name: invarith

on:
  pull_request:
  push:
    branches: [main]

jobs:
  invarith:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 2
      - uses: astral-sh/setup-uv@v10
      - run: uv sync --locked
      - uses: invarith/action@v0.1.0
        with:
          python: .venv/bin/python
```

A project installed with pip, after `actions/setup-python`, can leave `python` at its default.

- **When it runs:** on pull requests, and on pushes to the default branch. A push's run is what moves invarith's record of the claims forward once a change merges.
- **After the project's dependencies are installed.** The harnesses import the project's code with its dependencies, on the interpreter the `python` input names. Install them the way the project's own CI does.
- **`fetch-depth: 2`.** On a pull request, the checkout is GitHub's merge commit, and invarith compares it with its first parent, the base.
- **Permissions:** `id-token: write`, so the job can sign in to invarith with its OIDC token, and otherwise only `contents: read`. No secret is needed.
- **Pin the action** to a full release tag, such as `invarith/action@v0.1.0`, or to a release's commit SHA. A commit between releases names no invarith release, and fails saying so.
- **Never run it under `pull_request_target`,** which would run a fork's code with this repository's permissions. The action refuses to.
- **Linux runners only,** on x86_64 or arm64, GitHub-hosted or self-hosted. The runner needs `curl`, `tar` and `sha256sum`.

## Inputs

| Input | Default | What it is |
|---|---|---|
| `python` | `python` | The project's Python interpreter, with the project's dependencies installed: a name on the PATH, or a path, such as `.venv/bin/python` |
| `project` | `.` | The project's directory, holding `INVARITH.md` |
| `minutes` | `15` | The run's total time, in minutes |
| `server` | invarith's | The invarith server's address |

## Pull requests from forks

GitHub gives a fork's workflows no OIDC token, so invarith doesn't verify a pull request from a fork. The action exits without verifying, and invarith's App posts a neutral check run saying so. The default-branch run checks the code once it merges. To verify it before then, a maintainer can push the branch to the repository and open a pull request from there.

## What it does

1. **`bootstrap.sh`** downloads uv, at the version it pins, from uv's GitHub releases, and checks it against the SHA-256 it lists. uv then runs `bootstrap.py` on a Python it provides.
2. **`bootstrap.py`:**
   1. exits, with a notice, on a pull request from a fork;
   2. reads the invarith release this action release names, from `release/`;
   3. asks the job's OIDC token service for a token for invarith's audience, and invarith's server for short-lived links to that release's packages;
   4. downloads each package, and checks it against the SHA-256 in `release/`;
   5. installs them under `$RUNNER_TEMP/invarith`, never into the project's environment:
      - the CLI and the verifier in a virtual environment of their own;
      - the Python harness runtime in a directory of their own, for the project's interpreter;
   6. runs `invarith check`, whose exit code is the step's: 0 for success or neutral, 1 for failure, and 2 when the run can't be checked.

Nothing is resolved from a package index. The third-party packages the tools need are pinned in `release/`, each with its hashes, and installed with `--require-hashes`; invarith's own packages are installed only once their SHA-256s check out.

Each job downloads uv, a Python and those packages: tens of megabytes, from GitHub and the Python Package Index.

## Releases

Each release of this action names one invarith release, in `release/`:

- **`invarith.json`:** the invarith release's version, and each of its packages with its SHA-256;
- **`cli.txt`:** the third-party packages the CLI and the verifier need, pinned with their hashes, as the invarith release's lockfile has them;
- **`runtime.txt`:** the third-party packages the Python harness runtime needs, such as Hypothesis, pinned the same way.

`cli.txt` and `runtime.txt` are in the requirements format `uv export` writes.

## Development

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

The tests stand in for GitHub's token service and invarith's server. Two of them install small wheels built in the test, for real, with uv.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
