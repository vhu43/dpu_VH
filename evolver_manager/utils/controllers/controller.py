"""Abstract class"""
from abc import ABC, abstractmethod
from typing import Iterable

class Controller(ABC):
  """An abstract class defining a control law interface.


  Attributes:
    setpoint: Defines a float that is the setpoint for the controller.
  """
  def __init__(self, parameters: Iterable[float], setpoint: float, rate: float):
    self._parameters = tuple(parameters)
    self.setpoint = setpoint
    self._rate = rate

  @abstractmethod
  def get_response(self, error):
    """A method that returns the result of a value"""
    raise NotImplementedError("Subclasses should implement this")
