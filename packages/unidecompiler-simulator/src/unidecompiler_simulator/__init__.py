"""Decoupled, bounded execution of unidecompiler generic IR."""

from unidecompiler_simulator.adapters import (
    CallRequest,
    IntrinsicCall,
    NotHandled,
    ResolvedFunction,
    SimulationTarget,
    SimulationTargetCandidate,
    SimulationAdapter,
)
from unidecompiler_simulator.engine import (
    SimulationEngine,
    SimulationCancellation,
    SimulationEvent,
    SimulationLimits,
    SimulationResult,
    SimulationStatus,
    SimulationTargetListing,
    PreparedSimulationTarget,
)
from unidecompiler_simulator.environment import (
    ExternalCallRequest,
    ExternalCallResult,
    ExternalCallStatus,
    ExternalEnvironment,
    ExternalFunction,
)
from unidecompiler_simulator.values import ObjectValue, SliceValue, TableValue, UNDEFINED, UndefinedValue

__all__ = [
    "NotHandled",
    "CallRequest",
    "IntrinsicCall",
    "ObjectValue",
    "SliceValue",
    "TableValue",
    "UNDEFINED",
    "UndefinedValue",
    "ResolvedFunction",
    "SimulationAdapter",
    "SimulationTarget",
    "SimulationTargetCandidate",
    "SimulationEngine",
    "SimulationCancellation",
    "SimulationEvent",
    "SimulationLimits",
    "SimulationResult",
    "SimulationStatus",
    "SimulationTargetListing",
    "PreparedSimulationTarget",
    "ExternalCallRequest",
    "ExternalCallResult",
    "ExternalCallStatus",
    "ExternalEnvironment",
    "ExternalFunction",
]
