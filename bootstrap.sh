#!/usr/bin/env bash
# invarith's CI step, first part (README.md; invarith's decision D55): fetches a
# pinned uv for this runner, checks it against the SHA-256 listed here, and has it run
# bootstrap.py on a Python it provides. Everything it writes is under
# $RUNNER_TEMP/invarith.
#
# uv's version and checksums are from uv's GitHub releases, `uv-<target>.tar.gz.sha256`
# beside each archive. Dependabot doesn't update them: they change by hand, with
# UV_VERSION.
set -euo pipefail

UV_VERSION=0.13.7

if [ -z "${RUNNER_TEMP:-}" ] || [ -z "${GITHUB_ACTION_PATH:-}" ]; then
  echo "::error title=invarith::RUNNER_TEMP and GITHUB_ACTION_PATH aren't set: invarith/action runs only in a GitHub Actions job."
  exit 2
fi

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)
    target=x86_64-unknown-linux-gnu
    sha256=c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83
    ;;
  Linux-aarch64 | Linux-arm64)
    target=aarch64-unknown-linux-gnu
    sha256=71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc
    ;;
  *)
    echo "::error title=invarith::invarith runs on Linux runners, on x86_64 or arm64, and this runner is $(uname -s) on $(uname -m)."
    exit 2
    ;;
esac

home="$RUNNER_TEMP/invarith"
mkdir -p "$home"
archive="$home/uv-$target.tar.gz"
url="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$target.tar.gz"

if ! curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
  --retry 3 --output "$archive" "$url"; then
  echo "::error title=invarith::uv $UV_VERSION couldn't be downloaded from GitHub."
  exit 2
fi
if ! echo "$sha256  $archive" | sha256sum --check --status; then
  echo "::error title=invarith::uv $UV_VERSION's download doesn't match its SHA-256."
  exit 2
fi
tar --extract --gzip --file "$archive" --directory "$home"

# The project's interpreter, found on the job's PATH now: uv puts its own Python first
# on the PATH of what it runs. A relative path is made absolute, without following
# links, since a virtual environment's interpreter is one. One that isn't found is
# left as given, for bootstrap.py, since a TypeScript project needs none (invarith's
# decision D84).
given="${INVARITH_PYTHON:-python}"
if found="$(command -v -- "$given")"; then
  case "$found" in
    /*) ;;
    *) found="$PWD/$found" ;;
  esac
  export INVARITH_PYTHON="$found"
else
  export INVARITH_PYTHON="$given"
fi
# The job's own PATH, which invarith check runs with.
export INVARITH_JOB_PATH="$PATH"

export UV_CACHE_DIR="$home/cache"
export UV_PYTHON_INSTALL_DIR="$home/python"
exec "$home/uv-$target/uv" run --no-project --no-config --python 3.15 \
  "$GITHUB_ACTION_PATH/bootstrap.py"
