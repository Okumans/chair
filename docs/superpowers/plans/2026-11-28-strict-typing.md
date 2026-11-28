# Strict Typing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `chair_mqtt/` and `tools/` pass pyright strict with 0 errors, the wheel ships `py.typed` with a 100% typed public API, `make check` enforces both, and the package is released as 1.1.0.

**Architecture:** Pinned pyright and stubs live in a uv dev group. Modules are typed in dependency order, leaves first, so each task's files check clean against already-typed imports. The typecheck joins `make check` only in the last task: the pre-commit hook runs `make check`, so adding it earlier would block every intermediate commit.

**Tech Stack:** pyright 1.1.414 (strict), uv dependency groups, Pydantic 2, paho-mqtt 2 (ships `py.typed`), `types-jsonschema`, `types-pyserial`.

**Spec:** `docs/superpowers/specs/2026-11-28-strict-typing-design.md`

## Global Constraints

- pyright strict for `chair_mqtt/` and `tools/`; `tests/` excluded; `pythonVersion = "3.11"` (no 3.12+ syntax such as `type X = ...` or PEP 695 generics).
- No behaviour change. All 167+ existing tests stay green and unchanged, except where a test touches a private helper that a task renamed.
- Don't change Pydantic model field types in ways that change validation. Annotation-only precision such as `Literal` is allowed only where the model already rejects the other values.
- Suppressions use the narrow form `# pyright: ignore[<rule>]  # <reason>`. No bare `# type: ignore`, no file-level relaxations, and no `Any` or `cast` unless a JSON or third-party boundary needs it and is narrowed straight after.
- Per-task gate: `uv run --locked pyright <task files>` → `0 errors`, then `make check` → `OK`.
- Commit messages end with the repo's Co-Authored-By line.

## Review Focus

1. A "typing fix" that changes runtime behaviour, such as a new `None` check that raises where the code used to continue: every task runs the full suite, and the final reviewer diffs control flow, not just annotations.
2. Public signatures narrowed by mistake. For example, `on_event` handlers may be sync or async, and annotating them as only one would break FocusGuard. Task 6 adds a test that registers both kinds through the typed API, and pyright checks `tests/typing_usage.py`.
3. Pydantic validation drift from changed field annotations: the existing model and contract tests must stay green, and Task 2 adds a test that the JSON schema output for the models is unchanged.
4. 3.12-only typing syntax slipping in while CI runs 3.14: `pythonVersion = "3.11"` in the pyright config catches it. Task 1 test `test_pyright_targets_311`.
5. The wheel missing `py.typed`, which would silently make all the work invisible to FocusGuard: Task 1 test `test_wheel_ships_py_typed`.

---

### Task 1: Tooling and marker

**Files:**
- Modify: `pyproject.toml` (`[dependency-groups] dev`, `[tool.pyright]`, package-data), `uv.lock`, `Makefile` (new `typecheck` target, **not** in `check` yet)
- Create: `chair_mqtt/py.typed` (empty)
- Test: `tests/test_packaging.py`

**Interfaces:**
- Produces:
  - `make typecheck`, which runs `uv run --locked pyright` and then `uv run --locked pyright --verifytypes chair_mqtt --ignoreexternal`.
  - `[tool.pyright]` with `include = ["chair_mqtt", "tools"]`, `exclude = ["tests"]`, `typeCheckingMode = "strict"` and `pythonVersion = "3.11"`.
  - The dev group: `pyright==1.1.414`, `types-jsonschema`, `types-pyserial`, each pinned to the version `uv lock` resolves.

- [ ] **Step 1: Write failing tests** in `tests/test_packaging.py`:
  - `test_wheel_ships_py_typed`: `uv build --wheel --out-dir <tmp>`, then `zipfile` → `"chair_mqtt/py.typed"` is in `namelist()`, and so is `"chair_mqtt/mqtt.schema.json"`.
  - `test_pyright_targets_311`: `tomllib` on `pyproject.toml` → `["tool"]["pyright"]["pythonVersion"] == "3.11"`, `["typeCheckingMode"] == "strict"`, `"tests" in ["exclude"]`.
- [ ] **Step 2: Run** `uv run --locked python -m unittest tests.test_packaging -v`. Expected: FAIL on both.
- [ ] **Step 3: Implement** the config, marker, dev group, `uv lock` and the Makefile target.
- [ ] **Step 4: Run** `uv run --locked python -m unittest tests.test_packaging -v` → OK, and `make check` → OK. Record the baseline: `uv run --locked pyright | tail -1` should report about 2,900 errors.
- [ ] **Step 5: Commit** `build: add strict pyright tooling and the py.typed marker`.

### Tasks 2–7: Type each module group

Every task has the same shape:
1. Run `uv run --locked pyright <files>` to record the starting count.
2. Annotate and fix until that command reports `0 errors`.
3. Run `make check` → OK.
4. Commit `refactor: type <group> strictly`.

Each task names the precise types later tasks rely on.

- **Task 2: Leaves.** `chair_mqtt/models.py`, `protocol_bridge.py`, `transport.py`, `connection.py`.
  - Produces: the Pydantic models, annotated as they are; JSON wire values typed as `JsonObject = dict[str, object]` (defined in `models.py`, used by every later task); `BrokerConfig` and the connection factory fully annotated.
  - Add `tests/test_packaging.py::test_model_schema_unchanged`. It snapshots `model_json_schema()` for every model in `chair_mqtt.__all__` into `tests/data/model_schemas.json`. Generate the snapshot **before** editing `models.py`, so it fails if validation drifts.
- **Task 3: Contract and storage core.** `contract.py`, `store.py`, `serial_gateway.py`, `gateway.py`.
  - Produces: typed `route()`/`validate()` returning precise result types, and `Store` methods with annotated SQLite row access.
  - Fix the existing basic-mode error: `Store.accept` parameter-name mismatch (`m` vs `message`). Rename the base parameter to `message`; it's private-call only.
- **Task 4: Coordination.** `coordinator.py`, `controller.py`, `backend.py`.
- **Task 5: Bridge service.** `bridge.py`, `service.py`.
  - This group holds most of the basic-mode Optional-access errors. Narrow each with an explicit check that keeps the current behaviour: where the code assumed non-`None`, use `assert x is not None` with a comment giving the invariant, not a new branch.
- **Task 6: Public client API.** `async_storage.py`, `client.py`.
  - Produces: handler type aliases in `client.py`:
    - `EventHandler = Callable[[SensorEvent], None | Awaitable[None]]`
    - and the same shape for state, movement, bridge-status and command-error handlers.
    - `set_config` returns `CommandHandle`.
  - Create `tests/typing_usage.py`, a FocusGuard-style usage that registers one sync and one async handler of each kind and calls `set_config`. It is excluded from unittest discovery (no `test_` prefix).
  - Add `tests/test_packaging.py::test_typed_usage_checks`, which runs `uv run --locked pyright tests/typing_usage.py` → exit 0 (strict via the inline `# pyright: strict` comment).
- **Task 7: Console, package root and tools.** `console.py`, `__init__.py`, `tools/radio_window_bench.py`, `tools/transport.py`, `tools/mqtt_console.py`, `tools/release_version.py`.

### Task 8: Enforce, version and document

**Files:** `Makefile` (`check: format-check lint typecheck test`), `pyproject.toml` (`version = "1.1.0"`), `uv.lock`, `docs/MQTT_LIBRARY.md`, `docs/RELEASING.md`

- [ ] **Step 1:** Run `uv run --locked pyright` → `0 errors, 0 warnings`, then `uv run --locked pyright --verifytypes chair_mqtt --ignoreexternal`. Expected: `Type completeness score: 100%`. Fix anything left before continuing.
- [ ] **Step 2:** Add `typecheck` to `check`, bump to `1.1.0`, run `uv lock`, then `make check` → OK.
- [ ] **Step 3: Docs.**
  - `MQTT_LIBRARY.md`: a short "Typing" section saying the package ships `py.typed`, the public API is fully typed, and the handler aliases from Task 6 exist.
  - `RELEASING.md`: the `check` job now includes pyright strict.
- [ ] **Step 4:** Commit `build: enforce strict typing in make check and bump to 1.1.0`, push, then watch the CI run. Expected: `check` and `firmware` both succeed.

After merge, tag `v1.1` on `main` and push it, with maintainer approval. The release workflow attaches `chair_mqtt-1.1.0-py3-none-any.whl`.
