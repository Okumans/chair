# Releasing and CI

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

- **check** runs `make check`, the same command as the pre-commit hook: clang-format,
  ruff, the shell syntax check, strict pyright and `--verifytypes` (`make typecheck`),
  and all tests, including the native C++ tests.
  `mosquitto` and `openssl` are installed, so the broker and TLS tests run instead of
  being skipped.
- **firmware** builds the `sensor` and `gateway` images with PlatformIO.

The runner image and tool versions are pinned: `ubuntu-26.04`, uv 0.9.17, clang-format
23.1.5 and ruff 0.17.6 in `.github/actions/setup-checks/action.yml`, and PlatformIO 6.2.0
in `ci.yml`. Upgrade them on purpose,
together with your local tools, so CI and the pre-commit hook format code the same way.

## Cutting a release

1. Set `version` in `pyproject.toml`, run `uv lock`, and merge the change into `main`.
2. Tag the merged commit and push the tag:

   ```sh
   git tag -a v1.1 -m "v1.1" origin/main
   git push origin v1.1
   ```

`.github/workflows/release.yml` then:

1. Checks that the tag names the package version. `v1.1` matches `1.1.0`; a mismatch
   fails before anything is built (`tools/release_version.py`).
2. Runs `make check`.
3. Builds the `chair-mqtt` wheel.
4. Creates the GitHub release if it does not exist and attaches the wheel.

An attached wheel is never replaced: re-running the workflow for a tag that already
has the wheel fails. Downstream lockfiles pin the wheel's hash, so a broken release is
fixed by releasing a new patch version, never by re-uploading. To build assets for an
existing tag that has none, run:

```sh
gh workflow run release.yml -f tag=v1.1
```

## Using the wheel

Depend on the wheel attached to the release; this needs no git or build step:

```toml
chair-mqtt = { url = "https://github.com/Okumans/chair/releases/download/v1.0/chair_mqtt-1.0.0-py3-none-any.whl" }
```

uv records the wheel's hash in the consumer's lockfile.
