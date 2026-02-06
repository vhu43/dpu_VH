"""Abstract class for fits to be used with evolver

fit is a class to be extended by other fit types to be used for callibration of
evolver continuous culturing device. Implemented should be a fit equation and
also a fit initial value generation method.
"""

from abc import ABC, abstractmethod
from typing import Iterable


class Fit(ABC):
    """Fit class to be extended for evolver callibration files

    fit method that allows for checking of paramters, and defines equation to use
    for fitting.

    Attributes:
      parameters: A tuple containing the parameters to fit with
    """

    _NUM_PARAMS = -1
    _EQ_STRING = ""
    _LATEX_STRING = ""

    def __init__(self, parameters):
        """Inits fit with no parameters."""
        if not isinstance(parameters, Iterable):
            self.parameters = tuple([parameters])
        else:
            self.parameters = tuple(parameters)

        if len(self.parameters) != self._NUM_PARAMS:
            raise ValueError(f"There should be {self._NUM_PARAMS} parameters")

    def get_val(self, x) -> float:
        """Returns a value using the saved parameter"""
        return self.equation(x, *self.parameters)

    def get_inverse(self, y) -> float:
        """Returns the inverse using the saved parameter"""
        return self.inverse(y, *self.parameters)

    @classmethod
    def fit_equation_str(cls, latex=False) -> str:
        """Method to class constant referring to fit equation"""
        if latex:
            return cls._LATEX_STRING
        return cls._EQ_STRING

    @classmethod
    @abstractmethod
    def equation(cls, x):
        raise NotImplementedError("Subclasses should implement the fit equation")

    @classmethod
    @abstractmethod
    def inverse(cls, y):
        raise NotImplementedError("Subclass should implement inversion for runtime")

    @classmethod
    @abstractmethod
    def grad(cls, x):
        raise NotImplementedError("Subclass should implement gradient for stats")

    @classmethod
    @abstractmethod
    def get_bounds(cls, x, y):
        raise NotImplementedError("Subclasses should implement Bounds for fit")

    @classmethod
    @abstractmethod
    def get_initial_values(cls, x, y):
        raise NotImplementedError("Subclasses should implement initial values")
