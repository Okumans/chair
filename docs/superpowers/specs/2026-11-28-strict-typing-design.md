# Strict typing for chair_mqtt — design

## Intent

`chair-mqtt` should be a fully typed modern Python package. FocusGuard consumes it
from a release wheel and should see complete types in editors and type checkers
(`SensorEvent` through `on_event`, `set_config` arguments, and so on). Strict type
checking also catches real bugs in the bridge and store, such as `None` used without
a check. It ships as v1.1.

## Goals / non-goals / anti-goals

- Goal: the wheel ships `py.typed`; `pyright --verifytypes chair_mqtt --ignoreexternal`
  reports 100% known types for the public API.
- Goal: `pyright` **strict** reports 0 errors for `chair_mqtt/` and `tools/`, and
  `make check` (and therefore CI and the pre-commit hook) enforces it.
- Non-goal: type-checking `tests/` (excluded by the maintainer).
- Non-goal: switching to mypy, or adding runtime type enforcement beyond the existing
  Pydantic models.
- Anti-goal: any behaviour change. Typing work may restructure code only where needed
  for the types to be sound; every existing test must stay green unchanged, except for
  tests that touch renamed private helpers.
- Anti-goal: blanket `# type: ignore` / `cast` / `Any` to silence errors. Each
  remaining suppression is narrow (one rule, one line) and has a reason comment.

## Approaches (chosen by the maintainer)

- A. A fully typed public API plus basic mode everywhere. Smaller, but leaves
  internals loosely checked.
- **B. Strict across `chair_mqtt/` and `tools/` (picked).** The modern-package norm,
  and the maintainer accepts the risk. The baseline is about 2,700 strict findings in
  `chair_mqtt/` and 215 in `tools/`; most are missing parameter or return annotations
  and the unknown types they cause.
- C. The `py.typed` marker plus basic mode only. Too weak; most of the API would stay
  `Unknown`.

## Design

- **Checker:** pyright, pinned in a `[dependency-groups] dev` group in `pyproject.toml`
  (with `uv.lock`) so CI and local runs use the same version (1.1.414 at the time of
  writing).
  - Stubs in the same group: `types-jsonschema` and `types-pyserial`. paho-mqtt 2.x
    already ships `py.typed`.
- **Config:** `[tool.pyright]` in `pyproject.toml`:
  - `include = ["chair_mqtt", "tools"]` and `exclude = ["tests"]`
  - `typeCheckingMode = "strict"` and `pythonVersion = "3.11"`
- **Makefile:**
  - A new `typecheck` target runs `uv run --locked pyright`, then
    `uv run --locked pyright --verifytypes chair_mqtt --ignoreexternal`.
  - `check` gains `typecheck`.
  - The CI setup needs no change beyond the dev group being installed by `uv run`.
- **Package:** add `chair_mqtt/py.typed` to `[tool.setuptools.package-data]`.
- **Code:**
  - Annotate every function and attribute.
  - Fix the basic-mode findings (Optional access, private imports, override
    mismatches) properly.
  - Prefer precise types (`TypedDict`, `Protocol`, `Literal`, Pydantic models) over
    `dict[str, Any]` where the shape is known.
  - The JSON wire boundary may use `dict[str, object]` or `Any` with narrowing.
- **Version:** `1.1.0` in `pyproject.toml`; tag `v1.1` after merge.
- **Docs:** `docs/MQTT_LIBRARY.md` states the package is fully typed;
  `docs/RELEASING.md` mentions the typecheck in CI.

## Left to the builder

The order of modules, the internal type names, where `Protocol`s are worth
introducing, and the per-line justifications for any unavoidable suppression (for
example, gaps in third-party stubs).
