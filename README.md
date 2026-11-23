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
      - uses: invarith/action@v0.10.0
        with:
          python: .venv/bin/python
```

A project installed with pip, after `actions/setup-python`, can leave `python` at its default.

A TypeScript or JavaScript project, one with a `package.json` in the project's directory, sets up Node.js and installs its packages with `npm ci` instead, with an action release from v0.8.0 on, the first that holds invarith's TypeScript runtime:

```yaml
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 2
      - uses: actions/setup-node@v7
        with:
          node-version: 24
          # The newest 24, not an older one a runner has cached: v0.10.0 needs 24.11 or later.
          check-latest: true
          cache: npm
      - run: npm ci
      - uses: invarith/action@v0.10.0
```

A Java project, one with a Gradle or Maven build file in the project's directory, sets up its JDK and builds the project before the invarith step, in the same job, with an action release from v0.9.0 on, the first that holds invarith's Java tools:

```yaml
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 2
      - uses: actions/setup-java@v6
        with:
          distribution: temurin
          java-version: 21
          cache: gradle
      - run: ./gradlew build
      - uses: invarith/action@v0.10.0
```

A Maven project builds with `./mvnw --batch-mode verify`, or `mvn`, with `cache: maven`.

- **When it runs:** on pull requests, and on pushes to the default branch. A push's run is what moves invarith's record of the claims forward once a change merges.
- **After the project's dependencies are installed.** The harnesses import the project's code with its dependencies, on the interpreter the `python` input names, or, for a TypeScript project, on the Node.js the `node` input names, from the project's `node_modules`, or, for a Java project, on the JDK the `java` input names, with the classpath the project's build gives. Install them, or build the project, the way the project's own CI does.
- **A TypeScript project** needs the Node.js that the TypeScript runtime's pinned packages need: for v0.8.0 to v0.10.0, 24.11 or later, or 22.18 or later in the 22 line, as its @babel/parser requires. From v0.10.0, on another release, such as 22.17 or 23, each claim bound in TypeScript is unknown, and its reason names the releases the harnesses need; the project's other claims are checked as before. It needs npm too, which the action installs invarith's TypeScript runtime with, from the npm registry, reading none of the job's npm configuration, such as a registry `actions/setup-node` names for the project's own packages. It needs no Python: if `python` is left at its default, and the runner has no `python`, or one the Python runtime can't be installed for, the action checks the project without its Python runtime, and says so. A project whose `package.json` isn't for the code its contract covers, such as a Python project's formatting tools, can leave the TypeScript runtime out with `skip-languages: typescript`.
- **A Java project** needs a JDK: the `java` the `java` input names, by default the one `actions/setup-java` puts first on the PATH, with a `javac` beside it, which compiles the harnesses. A `java` that's a link, such as `/usr/bin/java`, is followed to the JDK's own. It should be the JDK the project builds with, since the harnesses run the classes its build compiled. With the `java` input left at its default and no JDK found, the action checks the project without its Java tools, and a warning says so; the project then needs a Python interpreter, as a Python project does, and with neither, the step stops, saying no JDK was found. A `java` input the workflow sets, even to `java`, must be a JDK's. GitHub's hosted Ubuntu runners have a JDK on the PATH, so there, a project with a Gradle or Maven build file has its build run by the step even if its workflow doesn't build it, such as a Python project with a `pom.xml` for another part of the repository. Such a project sets `skip-languages: java`, and the action leaves the Java tools out, with a notice. Once the runtimes are installed, the action asks the project's own Gradle or Maven build for its classpath, where it is, with `invarith setup --in-place` (invarith/backend's decision D86). That runs the build again, with the caches the workflow's build used, so it's mostly up to date if the workflow built the project in the same job. It runs on the job's PATH, without the job's means of asking for OIDC tokens. A build that can't give the classpath doesn't stop the step, since a project's build files needn't mean its claims are bound in Java: the check runs without it, each claim bound in Java is unknown, and a warning says so. The build gets at most the `minutes` input's time; one still running then is stopped, and the check runs without the classpath, as for a build that can't give it. A Java project needs no Python either, as a TypeScript project doesn't.
- **`fetch-depth: 2`.** On a pull request, the checkout is GitHub's merge commit, and invarith compares it with its first parent, the base.
- **Permissions:** `id-token: write`, so the job can sign in to invarith with its OIDC token, and otherwise only `contents: read`. No secret is needed.
- **Pin the action** to a full release tag, such as `invarith/action@v0.10.0`, or to a release's commit SHA. A release's tag is the version of the invarith release it runs. A commit between releases runs the release merged before it, with a bootstrapper no release named; one before the first release names none, and fails saying so.
- **Never run it under `pull_request_target`,** which would run a fork's code with this repository's permissions. The action refuses to.
- **Linux runners only,** on x86_64 or arm64, GitHub-hosted or self-hosted. The runner needs `curl`, `tar` and `sha256sum`.

## Inputs

| Input | Default | What it is |
|---|---|---|
| `python` | `python` | The project's Python interpreter, with the project's dependencies installed: a name on the PATH, or a path, such as `.venv/bin/python` |
| `node` | `node` | For a TypeScript or JavaScript project, one with a `package.json`, the project's Node.js: a name on the PATH, or a path |
| `java` | the `java` on the PATH | For a Java project, one with a Gradle or Maven build file, the project's JDK's `java`, with a `javac` beside it: a name on the PATH, or a path. If the workflow sets it, even to `java`, it must be a JDK's |
| `skip-languages` | none | The languages whose runtimes the action leaves out, though the project has their files, such as a Gradle build for another part of the repository: `java`, `typescript`, or both, separated by spaces or commas. A claim bound in a language left out isn't checked |
| `project` | `.` | The project's directory, holding `INVARITH.md` |
| `minutes` | `15` | The check's time, in minutes, which also limits a Java project's in-place build, before the check |
| `server` | invarith's | The invarith server's address |

## Pull requests from forks

GitHub gives a fork's workflows no OIDC token, so invarith doesn't verify a pull request from a fork. The action exits without verifying, and invarith's App posts a neutral check run saying so. The default-branch run checks the code once it merges. To verify it before then, a maintainer can push the branch to the repository and open a pull request from there.

## What it does

1. **`bootstrap.sh`** downloads uv, at the version it pins, from uv's GitHub releases, and checks it against the SHA-256 it lists. uv then runs `bootstrap.py` on a Python it provides.
2. **`bootstrap.py`:**
   1. exits, with a notice, on a pull request from a fork;
   2. reads the invarith release this action release names, from `release/`;
   3. finds the project's interpreter, and, for a TypeScript project, one with a `package.json`, its Node.js, and for a Java project, one with a Gradle or Maven build file, its JDK. Which files make a project need which runtime, the release's `invarith.json` lists, unless the `skip-languages` input leaves the language out;
   4. asks the job's OIDC token service for a token for invarith's audience, and invarith's server for short-lived links to that release's packages;
   5. downloads each package, and checks it against the SHA-256 in `release/`;
   6. installs them under `$RUNNER_TEMP/invarith`, never into the project's environment:
      - the CLI and the verifier in a virtual environment of their own;
      - the Python harness runtime in a directory of their own, for the project's interpreter;
      - for a TypeScript project, the TypeScript harness runtime in a directory of its own: its pinned packages, installed by the npm beside the project's Node.js, with `npm ci --ignore-scripts`, and the runtime's own package unpacked beside them;
      - for a Java project, the Java tools' jars in a directory of their own, never on the project's build path, and then the classpath the project's own build gives, where it is;
   7. runs `invarith check`, whose exit code is the step's: 0 for success or neutral, 1 for failure, and 2 when the run can't be checked.

Nothing is resolved from a package index. The third-party packages the tools need are pinned in `release/`, each with its hashes, and installed with `--require-hashes`, or, for the TypeScript runtime, each with its integrity, in an npm lockfile, which `npm ci` installs as written, running none of their install scripts; invarith's own packages are installed only once their SHA-256s check out. The Java runtime's jar holds its one third-party dependency, Byte Buddy, shaded into it, at the version invarith's build pins.

Each job downloads uv, a Python and those packages: tens of megabytes, from GitHub and the Python Package Index, and, for a TypeScript project, the npm registry. For a Java project, the project's build downloads whatever the workflow's caches don't hold, such as Gradle or Maven and the project's dependencies, from where the build gets them.

## Releases

Each release of this action runs one invarith release, and has its version: `invarith/action@vX.Y.Z` runs invarith X.Y.Z. A change to the action alone ships with the next invarith release (invarith/backend's decision D56). A release's files are in `release/`:

- **`invarith.json`:** the invarith release's version, and each of its packages with its SHA-256: its wheels, under `packages`; in a release that holds the TypeScript runtime, its npm package, under `typescript_runtime`; and in one that holds the Java tools, the Java runtime's jar and the Maven extension's, under `java_runtime`. Under `markers`, from v0.9.0 on, the files whose presence in a project's directory makes the action install each of those runtimes for it, as invarith's adapters name them (invarith/backend's decision D87). An earlier release, without them, makes a TypeScript project one with a `package.json`;
- **`cli.txt`:** the third-party packages the CLI and the verifier need, pinned with their hashes, as the invarith release's lockfile has them;
- **`runtime.txt`:** the third-party packages the Python harness runtime needs, such as Hypothesis, pinned the same way;
- **`typescript-runtime-lock.json`,** in a release that holds the TypeScript runtime: the third-party npm packages the TypeScript runtime needs, such as tsx and fast-check, as an npm lockfile, each pinned to a version, with the npm registry's URL for it and its integrity (invarith/backend's decision D82).

`cli.txt` and `runtime.txt` are in the requirements format `uv export` writes.

invarith's release workflow writes the four files and attaches them to invarith's own release. To make the matching release here:

1. On a branch from `main`, download them into `release/`:

   ```bash
   gh release download vX.Y.Z --repo invarith/backend --dir release --clobber --pattern invarith.json --pattern cli.txt --pattern runtime.txt --pattern typescript-runtime-lock.json
   ```

2. Open a pull request, "Release invarith X.Y.Z", and merge it once CI passes. CI checks the files (`tests/test_release.py`).
3. On that push to `main`, the release workflow (`.github/workflows/release.yml`) tags `vX.Y.Z` and creates a GitHub release, whose notes list invarith's packages with their SHA-256s, and the pinned packages (`release_notes.py`).

## Development

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

The tests stand in for GitHub's token service and invarith's server. Five of them install small packages built in the test, for real: wheels with uv; for a TypeScript project, npm packages with the npm beside the `node` on the PATH, which they skip without one; and for a small Java project, the Java tools' jars, with a stand-in CLI whose setup and check record how they were run, so no project is built. CI runs them on the Node.js release invarith's TypeScript image holds.

### Rules and updates

`.github/rulesets/action-main.json` defines the ruleset "CI must pass on main": a pull request can merge into `main` only once the `All jobs passed` check from GitHub Actions has succeeded. That job needs every other CI job, so adding or renaming a job doesn't change the ruleset. The org ruleset "Default branches" covers the rest of the default branch's protection. It's applied once, with:

```bash
gh api -X POST repos/invarith/action/rulesets --input .github/rulesets/action-main.json
```

After that, with the ruleset's ID from `gh api repos/invarith/action/rulesets`:

```bash
gh api -X PUT repos/invarith/action/rulesets/<id> --input .github/rulesets/action-main.json
```

Dependabot updates the actions CI uses, weekly (`.github/dependabot.yml`). uv's version and checksums in `bootstrap.sh` change by hand.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
