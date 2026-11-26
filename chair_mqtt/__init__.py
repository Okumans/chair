"""High-level MQTT facade for Smart Chair.

Exports load on first access, so the serial tools can import
``chair_mqtt.transport`` and ``chair_mqtt.protocol_bridge`` with only pyserial
installed.
"""

from importlib import import_module
from typing import TYPE_CHECKING

_EXPORTS = {
    "SQLiteStorage": ".async_storage",
    "Storage": ".async_storage",
    "BackendLedger": ".backend",
    "BridgeStatus": ".client",
    "ChairClient": ".client",
    "CommandError": ".client",
    "CommandHandle": ".client",
    "CommandState": ".client",
    "CommandUpdate": ".client",
    "SensorEvent": ".client",
    "SensorState": ".client",
    "ControllerLedger": ".controller",
    "ChairMqttBridge": ".facade",
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
    "BrokerConfig",
    "ChairClient",
    "ChairConfiguration",
    "ChairMqttBridge",
    "ClientConfig",
    "CommandError",
    "CommandHandle",
    "CommandNotice",
    "CommandReceipt",
    "CommandResult",
    "CommandState",
    "CommandUpdate",
    "ConfigRequest",
    "ConfigSet",
    "ControllerLedger",
    "EventAck",
    "MovementBatch",
    "MovementLoss",
    "MovementSample",
    "PublishResult",
    "RadioReady",
    "SQLiteStorage",
    "SeatEvent",
    "SensorEvent",
    "SensorState",
    "StateSync",
    "Storage",
]


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted({*globals(), *__all__})


if TYPE_CHECKING:
    from .async_storage import SQLiteStorage, Storage
    from .backend import BackendLedger
    from .client import (
        BridgeStatus,
        ChairClient,
        CommandError,
        CommandHandle,
        CommandState,
        CommandUpdate,
        SensorEvent,
        SensorState,
    )
    from .controller import ControllerLedger
    from .facade import ChairMqttBridge
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
