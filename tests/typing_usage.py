# pyright: strict
"""Static typing check for downstream consumers of chair_mqtt's public client API."""

from pathlib import Path

from chair_mqtt import (
    BridgeStatus,
    BrokerConfig,
    ChairClient,
    ChairConfiguration,
    ClientConfig,
    CommandError,
    CommandHandle,
    CommandState,
    CommandUpdate,
    MovementBatch,
    MovementLoss,
    SensorEvent,
    SensorState,
    SQLiteStorage,
)
from chair_mqtt.async_storage import SQLiteTransaction
from chair_mqtt.client import (
    BridgeStatusHandler,
    CommandErrorHandler,
    CommandUpdateHandler,
    EventHandler,
    MovementHandler,
    MovementLossHandler,
    StateHandler,
)


async def exercise_client(db_path: Path) -> None:
    chair = ChairClient(
        ClientConfig(node_id="chair-01", bridge_id="pi-01", consumer_id="backend-01"),
        SQLiteStorage(db_path),
        BrokerConfig(
            host="127.0.0.1", port=1883, client_id="focusguard", plaintext=True
        ),
    )

    @chair.on_event
    async def handle_event_sqlite(event: SensorEvent, tx: SQLiteTransaction) -> None:
        await tx.execute(
            "INSERT INTO events VALUES (?, ?)",
            (event.message.boot_id, event.message.seq),
        )

    async def handle_event_generic(event: SensorEvent, tx: object) -> None:
        _ = (event.publication_id, tx)

    event_cb: EventHandler = handle_event_generic
    chair.on_event(event_cb)
    _ = handle_event_sqlite

    @chair.on_state
    async def handle_state(state: SensorState) -> None:
        _ = state.boot_generation

    state_cb: StateHandler = handle_state
    chair.on_state(state_cb)

    @chair.on_movement
    async def handle_movement(batch: MovementBatch) -> None:
        _ = batch.samples

    movement_cb: MovementHandler = handle_movement
    chair.on_movement(movement_cb)

    @chair.on_movement_loss
    async def handle_loss(loss: MovementLoss) -> None:
        _ = loss.dropped_batches

    loss_cb: MovementLossHandler = handle_loss
    chair.on_movement_loss(loss_cb)

    @chair.on_command_update
    async def handle_update(update: CommandUpdate) -> None:
        _ = update.status

    update_cb: CommandUpdateHandler = handle_update
    chair.on_command_update(update_cb)

    @chair.on_bridge_status
    async def handle_status(status: BridgeStatus) -> None:
        _ = status.status

    status_cb: BridgeStatusHandler = handle_status
    chair.on_bridge_status(status_cb)

    @chair.on_command_error
    async def handle_error(error: CommandError) -> None:
        _ = error.reason

    error_cb: CommandErrorHandler = handle_error
    chair.on_command_error(error_cb)

    async with chair:
        handle: CommandHandle = await chair.set_config(
            ChairConfiguration(
                revision=1,
                heartbeat_s=900,
                movement_batch_s=3,
                movement_enabled=True,
                radio_windows_enabled=True,
                radio_interval_s=30,
                radio_open_s=5,
                radio_motion_tail_s=10,
            ),
            expires_in_seconds=60,
        )
        state: CommandState = await handle.wait(timeout=10.0)
        fetched: CommandState | None = await chair.get_command(handle.command_id)
        _ = (state.status, fetched, chair.connected, chair.ready)
