# CI and Wheel Releases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GitHub Actions runs `make check` and the firmware builds on every push and PR. Each `v*` tag gets the `chair-mqtt` wheel attached to its release.

**Architecture:** A composite action (`.github/actions/setup-checks`) installs the pinned toolchain once for both workflows. `ci.yml` runs checks and firmware builds. `release.yml` validates the tag against `pyproject.toml` using a small tested script (`tools/release_version.py`), reruns the checks, builds the wheel, and uploads it without ever overwriting an existing asset.

**Tech Stack:** GitHub Actions on `ubuntu-26.04`, uv, PlatformIO, `gh` CLI, Python 3 stdlib.

**Spec:** `docs/superpowers/specs/2026-11-28-ci-release-design.md`

## Global Constraints

- Runner `ubuntu-26.04`, never `ubuntu-latest`.
- Tools are pinned to `clang-format==23.1.5` and `ruff==0.17.6`, and uv is installed via `astral-sh/setup-uv`.
- The `check` job installs apt `mosquitto` and `openssl` so the broker and TLS tests are not skipped.
- The tag matches when `v` + the version compares equal with trailing `.0` padding (`v1.0` matches `1.0.0`; `v1.0.1` does not match `1.0.0`).
- A release asset is never overwritten (no `--clobber`); a name that already exists fails the job.
- `pyproject.toml` and `uv.lock` say `1.0.0`.
- Non-goals: firmware binaries on releases, PyPI.

## Review Focus

1. A tag that doesn't match the version (`v1.1` while the version is `1.0.0`) must fail before building. Task 1 test `test_mismatch`.
2. Malformed tags (`1.0` without `v`, `v1.0-rc1`, `vfoo`) must fail with a clear message rather than a traceback. Task 1 test `test_malformed`.
3. Re-running the release for an existing tag must not replace the wheel. Task 3 step: the upload checks `gh release view --json assets` first.
4. A release created by hand before the tag workflow runs (v1.0 already exists) must be reused, not duplicated. Task 3: `gh release view` → `create` only if missing.
5. On CI, the broker tests must actually run. Task 2 verification: the CI log must not contain `private Mosquitto broker unavailable`.

---

### Task 1: Version bump and tag check

**Files:**
- Modify: `pyproject.toml:7` (`version = "1.0.0"`), `uv.lock` (via `uv lock`)
- Create: `tools/release_version.py`
- Test: `tests/test_release_version.py`

**Interfaces:**
- Produces: `tools/release_version.py` with `def tag_matches(tag: str, version: str) -> bool`, which raises `ValueError` on a malformed tag, and a CLI `python tools/release_version.py <tag>`. The CLI reads `version` from `pyproject.toml` via `tomllib`, exits 0 on a match, and exits 1 with `tag v1.1 does not match pyproject version 1.0.0` or `malformed tag: ...` on stderr.

- [ ] **Step 1: Write the failing tests** in `tests/test_release_version.py` (unittest; import the module from `tools/` the way the other tool tests do):
  - `test_match`: `tag_matches("v1.0", "1.0.0")`, `("v1.0.0", "1.0.0")`, `("v2", "2.0.0")` are all True.
  - `test_mismatch`: `("v1.1", "1.0.0")` and `("v1.0.1", "1.0.0")` are False.
  - `test_malformed`: `"1.0"`, `"v1.0-rc1"`, `"vfoo"` and `""` raise `ValueError`.
  - `test_cli_reads_pyproject`: run the script as a subprocess with `v1.0` → exit 0; with `v9.9` → exit 1, and stderr contains `does not match`.
- [ ] **Step 2: Run** `uv run --locked python -m unittest tests.test_release_version -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement.** Tags match `^v(\d+(\.\d+)*)$`. Compare integer tuples after right-padding both with zeros to the same length.
- [ ] **Step 4: Bump** `pyproject.toml` to `1.0.0` and run `uv lock`. `uv.lock` should then show `name = "chair-mqtt"` `version = "1.0.0"`.
- [ ] **Step 5: Run** `make check`. Expected: all pass.
- [ ] **Step 6: Commit** `feat: check release tags against the package version`.

### Task 2: Shared setup and CI workflow

**Files:**
- Create: `.github/actions/setup-checks/action.yml` (composite)
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: the composite action `./.github/actions/setup-checks`. It installs apt `mosquitto openssl make`, runs `astral-sh/setup-uv`, and installs pinned `clang-format==23.1.5` and `ruff==0.17.6` with `uv tool install`. It prints `c++ --version`, `clang-format --version` and `ruff --version` so the log shows the toolchain.

- [ ] **Step 1: Write `ci.yml`.**
  - Triggers: `push` (all branches) and `pull_request`.
  - `concurrency` cancels superseded runs on the same ref.
  - Job `check` (ubuntu-26.04): checkout, setup-checks, `make check`.
  - Job `firmware` (ubuntu-26.04): checkout, `actions/setup-python`, `pip install platformio`, cache `~/.platformio` keyed on `hashFiles('platformio.ini')`, then `pio run -e sensor -e gateway`.
  - `permissions: contents: read`.
- [ ] **Step 2: Lint locally** with `actionlint` (`uvx --from actionlint-py actionlint`). Expected: no output.
- [ ] **Step 3: Commit** `ci: run checks and firmware builds on every push`, then push `ci/release`.
- [ ] **Step 4: Verify on GitHub.** Run `gh run watch` on the branch run. Expected: both jobs succeed, and the `check` log does not contain `private Mosquitto broker unavailable` or `openssl unavailable`.
  - If the C++ tests fail to compile on the runner's default `c++`, switch the `check` job to `container: archlinux:latest` (pacman `base-devel uv mosquitto openssl git`) and keep the pinned tools. Record that decision in the ledger.
  - If clang-format 23.1.5 isn't on PyPI, pin the newest available 23.x and check that `make format-check` still passes locally with it.

### Task 3: Release workflow

**Files:**
- Create: `.github/workflows/release.yml`

**Interfaces:**
- Consumes: `tools/release_version.py` (Task 1) and `./.github/actions/setup-checks` (Task 2).

- [ ] **Step 1: Write `release.yml`.**
  - Triggers: `push: tags: ['v*']`, plus `workflow_dispatch` with input `tag` (required) for re-running an existing tag.
  - `permissions: contents: write`.
  - One job on ubuntu-26.04, with these steps:
    1. Check out the tag (`ref: ${{ inputs.tag || github.ref_name }}`) and resolve it into `TAG`.
    2. `python3 tools/release_version.py "$TAG"`.
    3. setup-checks, then `make check`.
    4. `uv build --wheel`.
    5. `gh release view "$TAG"` or else `gh release create "$TAG" --verify-tag --title "$TAG" --generate-notes`.
    6. For each `dist/*.whl`: if `gh release view "$TAG" --json assets -q '.assets[].name'` already lists it, fail with `asset <name> already exists; refusing to replace`. Otherwise `gh release upload "$TAG" <file>`.
- [ ] **Step 2: Lint** with actionlint. Expected: no output.
- [ ] **Step 3: Dry-run the shell locally.** Run the version check with `v1.0` (exit 0) and `v1.1` (exit 1), and run `uv build --wheel`, which should produce `dist/chair_mqtt-1.0.0-py3-none-any.whl`. Remove `dist/` afterwards.
- [ ] **Step 4: Commit** `ci: attach the wheel to tagged releases` and push.

### Task 4: Release docs

**Files:**
- Create: `docs/RELEASING.md`
- Modify: `README.md` (link it in the docs list next to the MQTT bridge service entry)

- [ ] **Step 1: Write `docs/RELEASING.md`.** Cover:
  - Bump `pyproject.toml`, run `uv lock`, merge, then `git tag -a vX.Y -m ...` and push the tag.
  - What `release.yml` checks.
  - Assets are never replaced; to fix a broken release, cut a new patch version.
  - Re-running via `gh workflow run release.yml -f tag=vX.Y`.
  - FocusGuard's dependency form: `chair-mqtt = { url = "https://github.com/Okumans/chair/releases/download/vX.Y/chair_mqtt-X.Y.Z-py3-none-any.whl" }`.
  - CI pins `ubuntu-26.04`, clang-format and ruff; upgrade them deliberately.
- [ ] **Step 2: Verify** `make check` passes (the pre-commit hook runs it), then commit `docs: describe releases and CI`.

### After merge (maintainer approval required, outward-facing)

Move `v1.0` to the merged `main` commit (`git tag -fa v1.0 origin/main`, then `git push -f origin v1.0`). The release workflow should then attach `chair_mqtt-1.0.0-py3-none-any.whl` to the existing v1.0 release. If the tag push does not trigger it, run `gh workflow run release.yml -f tag=v1.0`.
