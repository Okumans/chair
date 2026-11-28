"""High-level MQTT library for Smart Chair.

Exports load on first access, so the serial tools can import
``chair_mqtt.transport`` and ``chair_mqtt.protocol_bridge`` with only pyserial
installed.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

_EXPORTS: dict[str, str] = {
    "SQLiteStorage": ".async_storage",
    "Storage": ".async_storage",
    "BackendLedger": ".backend",
    "BridgeStatus": ".client",
    "BridgeStatusHandler": ".client",
    "ChairClient": ".client",
    "CommandError": ".client",
    "CommandErrorHandler": ".client",
    "CommandHandle": ".client",
    "CommandState": ".client",
    "CommandUpdate": ".client",
    "CommandUpdateHandler": ".client",
    "EventHandler": ".client",
    "MovementHandler": ".client",
    "MovementLossHandler": ".client",
    "SensorEvent": ".client",
    "SensorState": ".client",
    "StateHandler": ".client",
    "ControllerLedger": ".controller",
    "ChairBridge": ".bridge",
    "GatewayStream": ".gateway",
    "GatewayTransport": ".gateway",
    "SerialTransport": ".serial_gateway",
    "BridgeConfig": ".models",
    "BridgeHealth": ".models",
    "BrokerConfig": ".models",
    "ChairConfiguration": ".models",
    "ClientConfig": ".models",
    "CommandNotice": ".models",
    "CommandReceipt": ".models",
    "CommandResult": ".models",
    "ConfigRequest": ".models",
    "ConfigSet": ".models",
    "EventAck": ".models",
    "MovementBatch": ".models",
    "MovementLoss": ".models",
    "MovementSample": ".models",
    "PublishResult": ".models",
    "RadioReady": ".models",
    "SeatEvent": ".models",
    "StateSync": ".models",
}

__all__ = [
    "BackendLedger",
    "BridgeConfig",
    "BridgeHealth",
    "BridgeStatus",
    "BridgeStatusHandler",
    "BrokerConfig",
    "ChairClient",
    "ChairConfiguration",
    "ChairBridge",
    "ClientConfig",
    "CommandError",
    "CommandErrorHandler",
    "CommandHandle",
    "CommandNotice",
    "CommandReceipt",
    "CommandResult",
    "CommandState",
    "CommandUpdate",
    "CommandUpdateHandler",
    "ConfigRequest",
    "ConfigSet",
    "ControllerLedger",
    "EventAck",
    "EventHandler",
    "GatewayStream",
    "GatewayTransport",
    "MovementBatch",
    "MovementHandler",
    "MovementLoss",
    "MovementLossHandler",
    "MovementSample",
    "PublishResult",
    "RadioReady",
    "SQLiteStorage",
    "SeatEvent",
    "SensorEvent",
    "SerialTransport",
    "SensorState",
    "StateHandler",
    "StateSync",
    "Storage",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


if TYPE_CHECKING:
    from .async_storage import SQLiteStorage, Storage
    from .backend import BackendLedger
    from .bridge import ChairBridge
    from .client import (
        BridgeStatus,
        BridgeStatusHandler,
        ChairClient,
        CommandError,
        CommandErrorHandler,
        CommandHandle,
        CommandState,
        CommandUpdate,
        CommandUpdateHandler,
        EventHandler,
        MovementHandler,
        MovementLossHandler,
        SensorEvent,
        SensorState,
        StateHandler,
    )
    from .controller import ControllerLedger
    from .gateway import GatewayStream, GatewayTransport
    from .models import (
        BridgeConfig,
        BridgeHealth,
        BrokerConfig,
        ChairConfiguration,
        ClientConfig,
        CommandNotice,
        CommandReceipt,
        CommandResult,
        ConfigRequest,
        ConfigSet,
        EventAck,
        MovementBatch,
        MovementLoss,
        MovementSample,
        PublishResult,
        RadioReady,
        SeatEvent,
        StateSync,
    )
    from .serial_gateway import SerialTransport
