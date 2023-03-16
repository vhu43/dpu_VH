"""A collection of type hint classes to aid in development.

THESE ARE NOT MEANT TO BE INITIALIZED. These are just references to get type
hints for various dictionaries passed around by the classes.
"""

from typing import TypedDict, List, Tuple


### Classes that handle evolver broadcasts and datatypes

class SensorCal(TypedDict):
  """Sensor calibration dictionary type hint.
  """
  name: str
  coefficeints: List
  type: str
  timeFit: float
  active: bool
  params: List

class Calibration(TypedDict):
  """Sensor calibration set dictionary type hint.
  """
  od: SensorCal
  temp: SensorCal
  pump: SensorCal

class ParamDict(TypedDict):
  """experimental param entry dictionary type hint.
  """
  fields_expected_incoming: int
  fields_expected_outgoing: int
  recurring: bool
  value: List

class ExperimentalParams(TypedDict):
  """experimental param config dictionary type hint.
  """
  lxml: ParamDict
  pump: ParamDict
  stir: ParamDict
  temp: ParamDict

class DataEntry(TypedDict):
  """Broadcast data dictionary type hint.
  """
  od_90: List
  od_135: List
  temperature: List

class DataDict(TypedDict):
  """Broadcast dictionary type hint.
  """
  data: DataEntry

class Broadcast(TypedDict):
  """Evolver Broadcast data type hint.
  """
  data: DataDict
  config: ExperimentalParams
  ip: str
  timestamp: float

class Record(TypedDict):
  """Record object for messages different x-stat devices can send.
  """
  vial: int
  command: str

class ChemostatRecord(Record):
  """Record object to record changes to chemorates.
  """
  IN1: Tuple[tuple[float, float]]
  IN2: Tuple[tuple[float, float]]
  OUT: Tuple[tuple[float, float]]

class TurbidostatRecord(Record):
  """Record object to record turbidostat pump commands
  """
  IN1: float
  IN2: float
  OUT: float

class MorbidostatRecord(Record):
  """Record object to record turbidostat pump commands
  """
  IN1: float
  IN2: float
  OUT: float
  growth_rate: float
  response: float

class UpdateMsg(TypedDict):
  """Return result of update methods.
  """
  time: float
  record: Tuple[Record]
  vials: Tuple[int]
  od: Tuple[float]
  temp: Tuple[float]

class EvolverInfo(TypedDict):
  """Return the info for connecting and identifying evolvers
  """
  name: str
  ip: str
  port: int
