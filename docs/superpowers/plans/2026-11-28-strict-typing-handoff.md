# Strict typing — handoff after Task 5

Plan: `docs/superpowers/plans/2026-11-28-strict-typing.md`
Spec: `docs/superpowers/specs/2026-11-28-strict-typing-design.md`
Branch: `feat/strict-typing` (local, not pushed). Head at handoff: `da2389c`.

## Done (Tasks 1–5, each reviewed and approved)

| Task | Commits | Result |
|---|---|---|
| 1 Tooling | `b0d4ab7` | pyright 1.1.414 and stubs in the uv `dev` group; strict `[tool.pyright]`; `chair_mqtt/py.typed`; `make typecheck` (**not yet** in `make check`); `tests/test_packaging.py` |
| 2 Leaves | `5155f1d`, `85f2933` | `models.py`, `protocol_bridge.py`, `transport.py`, `connection.py`; `JsonObject` added; model-schema snapshot test |
| 3 Contract/store | `5573278` | `contract.py`, `store.py`, `serial_gateway.py`, `gateway.py` |
| 4 Coordination | `abcceb2` | `coordinator.py`, `controller.py`, `backend.py` |
| 5 Bridge | `da5fb52`, `da2389c` | `bridge.py`, `service.py`; gateway hooks and store helpers renamed to public names |

`make check` passes with 170 tests. Pyright strict reports 0 errors on all of the files above.

## Remaining: 906 strict errors

| File | Errors | Plan task |
|---|---|---|
| `chair_mqtt/client.py` | 338 | Task 6 |
| `chair_mqtt/async_storage.py` | 217 | Task 6 |
| `chair_mqtt/console.py` | 196 | Task 7 |
| `tools/radio_window_bench.py` | 150 | Task 7 |
| `chair_mqtt/__init__.py` | 4 | Task 7 |
| `tools/mqtt_console.py` | 1 | Task 7 |

Then Task 8:
- Add `typecheck` to `check` in the Makefile.
- `pyright --verifytypes chair_mqtt --ignoreexternal` must reach 100%.
- Bump to `1.1.0` and run `uv lock`.
- Update the docs.
- Push and get CI green.

## Rules every task followed (keep them)

- **No behaviour change.** Keep exception types, messages, evaluation order, SQL, threading and return values. Tests stay unchanged, except for references to a private helper you rename.
- **Suppressions:** only `# pyright: ignore[<rule>]  # <reason>`, one rule on one line. No bare `# type: ignore` and no file-level relaxations. Keep them few, and prefer a real type (Protocol, TypedDict, `@overload`, Literal).
- **`Any` / `cast`:** only at a JSON, SQLite-row, TOML or third-party boundary, and only straight after an `isinstance` check.
- **Shared JSON type:** use `chair_mqtt.models.JsonObject` (`dict[str, object]`).
- **`contract.known_object/known_list/known_str/known_int`** narrow with `assert`, which `python -O` removes. Use them **only** on data that `validate()` or the schema has already checked, never as an input check.
- **Optional values the code already assumes are present:** use `assert x is not None` with a comment stating the invariant, not a new branch.
- **New helpers** get leading underscores; don't add public names unless they are part of the API.
- **Per-task gate:**
  1. `uv run --locked pyright <task files>` reports 0 errors.
  2. Every previously clean file still reports 0.
  3. `make check` passes. The pre-commit hook runs it, so each commit takes about 90 s.
- **Commit trailer:** end each commit with your own model's co-author line.

## Task-specific notes for Task 6 (`async_storage.py`, `client.py`)

- **Handler aliases (in the plan):** `EventHandler = Callable[[SensorEvent], None | Awaitable[None]]`, and the same shape for state, movement, bridge-status and command-error. `set_config` returns `CommandHandle`.
- **`tests/typing_usage.py`** (no `test_` prefix, so unittest doesn't pick it up) registers one sync and one async handler of each kind and calls `set_config`. Put `# pyright: strict` on its first line.
- **`test_packaging.py::test_typed_usage_checks`** runs `uv run --locked pyright tests/typing_usage.py` and expects exit 0.
- **`pending_receipts` row type:** give `Storage.pending_receipts` (`async_storage.py`) a precise row type. `client.py` already unpacks `(publication_id, topic, payload)`. The ledger methods `BackendLedger.pending_receipts` / `ControllerLedger.due` stay `list[Any]`, because the caller's `row_factory` decides the row shape.

## Small items reviewers left for the final pass (optional)

- `bridge.py`: none of the `_call` overloads exposes the `timeout` keyword, although the implementation accepts it.
- `bridge.py`: the `store._command_deadlines` suppression could go behind a small public store accessor.
- `bridge.py`: an `admit_command` `Literal` return type would remove the `disposition` suppression.
- The public-but-unexported `contract.known_*` helpers. Consider making them private when typing is finished.
