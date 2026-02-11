"""A collection of type hint classes to aid in development.

THESE ARE NOT MEANT TO BE INITIALIZED. These are just references to get type
hints for various dictionaries passed around by the classes.
"""

from __future__ import annotations

from typing import Any, List, Tuple, TypedDict

### Classes that handle evolver broadcasts and datatypes


class SensorCal(TypedDict):
    """Sensor calibration dictionary type hint."""

    name: str
    coefficients: List[list[float]]
    type: str
    timeFit: float
    active: bool
    params: List[str]
    calibrationType: str
    fits: list[dict[str, Any]]


class Calibration(TypedDict):
    """Sensor calibration set dictionary type hint."""

    od: SensorCal
    temp: SensorCal
    pump: SensorCal


class ParamDict(TypedDict):
    """experimental param entry dictionary type hint."""

    fields_expected_incoming: int
    fields_expected_outgoing: int
    recurring: bool
    value: List[str]


class ExperimentalParams(TypedDict):
    """experimental param config dictionary type hint."""

    lxml: ParamDict
    pump: ParamDict
    stir: ParamDict
    temp: ParamDict


class DataDict(TypedDict):
    """Broadcast data dictionary type hint.

    Keyed by param names from server conf.yml (od_90, od_135, temp, etc.).
    Each value is a list of 16 raw sensor readings (one per vial).
    """

    od_90: List[str]
    od_135: List[str]
    temp: List[str]


class Broadcast(TypedDict):
    """Evolver Broadcast data type hint."""

    data: DataDict
    config: ExperimentalParams
    ip: str
    timestamp: float


class Record(TypedDict):
    """Record object for messages different x-stat devices can send."""

    vial: int
    command: str


class ChemostatRecord(Record):
    """Record object to record changes to chemorates."""

    IN1: Tuple[float, float]
    IN2: Tuple[float, float]
    OUT: Tuple[float, float]


class TurbidostatRecord(Record):
    """Record object to record turbidostat pump commands"""

    IN1: float
    IN2: float
    OUT: float


class MorbidostatRecord(Record):
    """Record object to record turbidostat pump commands"""

    IN1: float
    IN2: float
    OUT: float
    growth_rate: float
    response: float


class UpdateMsg(TypedDict):
    """Return result of update methods."""

    time: float
    record: Tuple[Record, ...] | None
    vials: Tuple[int, ...]
    od: Tuple[float, ...]
    temp: Tuple[float, ...]


class EvolverInfo(TypedDict):
    """Return the info for connecting and identifying evolvers"""

    name: str
    ip: str
    port: int
