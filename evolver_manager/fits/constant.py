"""Implements a linear fit for evolver

Constant fit for evolver that extends fit class, implementing a get_val method
to return a value from a fit, a initial value method for numerically stable
constant fitting and for printing of latex string for matplotlib.
"""

import numpy as np

from . import fit


class Constant(fit.Fit):
    """Constant extending Fit class to implement Constant curve fitting operations

    Constant curve stores parametes for a 1-paramter model for a constant function

    Attributes:
      parameters: A tuple containing the parameters to fit with
      sensors: A tuple containing which data is fed into the fit

    """

    _NUM_PARAMS = 1
    _EQ_STRING = "a"
    _LATEX_STRING = "a"

    @classmethod
    def equation(cls, x, *params):
        a = params
        """Returns the result of the equation of the model"""
        return a * np.ones(np.array(x).shape)

    @classmethod
    def inverse(cls, y, *params):
        a = params
        """Returns the result of the inverse equation of the model"""
        return np.ones(np.array(y).shape) / a

    @classmethod
    def grad(cls, x, *params):
        x = np.array(x)
        del_a = np.ones(x.shape)  # Derivative with respect to 'a' is 1
        return np.stack([del_a]).reshape(cls._NUM_PARAMS, 1, -1)

    @classmethod
    def get_bounds(cls, x, y):
        """Defines bounds to try to fit the curve fitting"""
        del x, y
        return (-np.inf, np.inf)

    @classmethod
    def get_initial_values(cls, x, y):
        """Returns initial guess for parameters"""
        del x, y
        return (0,)
