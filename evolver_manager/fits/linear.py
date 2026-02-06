"""Implements a linear fit for evolver

Linear fit for evolver that extends fit class, implementing a get_val method
to return a value from a fit, a initial value method for numerically stable
Linear fitting and for printing of latex string for matplotlib.
"""

import numpy as np

from . import fit


class Linear(fit.Fit):
    """Linear extending Fit class to implement linear curve fitting operations

    Linear curve stores parametes for a 4-paramter model for translated
    Linear curve.

    Attributes:
      parameters: A tuple containing the parameters to fit with
    """

    _NUM_PARAMS = 2
    _EQ_STRING = "a + b * x"
    _LATEX_STRING = "a + bx"

    @classmethod
    def equation(cls, x, *params):
        """Returns the result of the equation of the model"""
        a, b = params
        return a + b * np.array(x)

    @classmethod
    def inverse(cls, y, *params):
        """Returns the result of the inverse equation of the model"""
        a, b = params
        return (np.array(y) - a) / b

    @classmethod
    def grad(cls, x, *params):
        """Returns the gradient of the log equation"""
        x = np.array(x)

        del params
        del_a = np.ones(x.shape)
        del_b = x
        return np.stack([del_a, del_b]).reshape(cls._NUM_PARAMS, 1, -1)

    @classmethod
    def get_bounds(cls, x, y):
        """Defines bounds to try to fit the curve fitting"""
        del x, y
        return (-np.inf, np.inf)

    @classmethod
    def get_initial_values(cls, x, y):
        """Returns initial guess for parameters"""
        slope = (np.max(y) - np.min(y)) / (np.max(x) - np.min(x))
        return (0, slope)
