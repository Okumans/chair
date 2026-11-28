# CI and wheel releases — design

## Intent

FocusGuard should install `chair-mqtt` from a wheel attached to a chair GitHub
release (`chair-mqtt = { url = ".../releases/download/v1.0/chair_mqtt-1.0.0-py3-none-any.whl" }`),
with no git or build step, and its lockfile pins the hash. Every push and PR also
runs the repository's checks automatically instead of relying only on the local
pre-commit hook.

## Goals / non-goals / anti-goals

- Goal: CI on every push and PR that runs `make check` (formatting, lint and all tests,
  including the broker and TLS tests) and builds both firmware images.
- Goal: on each `v*` tag, attach the built wheel to that tag's release.
- Non-goal: firmware binaries on releases; PyPI publishing.
- Anti-goal: an attached wheel ever changing. FocusGuard's lockfile pins its hash, so
  the workflow never overwrites an asset.
- Anti-goal: CI going red on an unchanged commit because a runner image or tool moved.

## Approaches (versioning of the already-published v1.0)

- **A. Bump to 1.0.0 and move the v1.0 tag to that commit (picked).** The release is
  minutes old and nothing pins it yet; a tag/version check prevents a repeat.
- B. Keep v1.0; first wheel at v1.0.1. Avoids rewriting a tag, but v1.0 stays wheel-less.
- C. setuptools-scm version from the tag. Never drifts, but adds a build dependency and
  cannot fix v1.0's existing commit.

## Design

- **`.github/workflows/ci.yml`**: `push` and `pull_request`.
  - Job `check` on `ubuntu-26.04` (pinned, not `ubuntu-latest`): apt `mosquitto`,
    `openssl`, `make`; uv via `astral-sh/setup-uv`; `clang-format==23.1.5` and
    `ruff==0.17.6` pinned to the maintainer's local versions; run `make check`. Its
    default `c++` is expected to handle C++23 and Glaze. If it cannot, fall back to an
    Arch Linux container for this job.
  - Job `firmware`: PlatformIO builds `sensor` and `gateway`, with `~/.platformio`
    cached.
- **`.github/workflows/release.yml`**: on a `v*` tag push.
  - Fail unless the tag equals `v` + the `pyproject.toml` version, comparing
    `X.Y` and `X.Y.0` as equal (so `v1.0` matches `1.0.0`).
  - Run `make check` (the same setup as CI), then `uv build --wheel`.
  - Create the release if it is missing, then upload the wheel. If an asset with
    that name already exists, fail; never `--clobber`.
- **`pyproject.toml`**: `version = "1.0.0"`; refresh `uv.lock` if it records the version.
- **Docs**: `docs/RELEASING.md` (bump the version, tag, push, what CI does, the wheel URL
  form for FocusGuard), linked from the README.
- **After merge** (maintainer-approved): force-move `v1.0` to the merged `main` commit and
  push it; the release workflow attaches the wheel to the existing v1.0 release.

## Left to the builder

The shared setup between `check` and `release` (a composite action or a reusable
workflow), the action versions, and the cache keys.
