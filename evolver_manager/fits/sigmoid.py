"""Implements a sigmoidal fit for evolver

Sigmoidal fit for evolver that extends fit class, implementing a get_val method
to return a value from a fit, a initial value method for numerically stable
sigmoidal fitting and for printing of latex string for matplotlib.
"""

import numpy as np

from . import fit


class Sigmoid(fit.Fit):
    """Sigmoid extending Fit class to implement sigmoidal curve fitting operations

    Sigmoidal curve stores parametes for a 4-paramter model for translated
    sigmoidal curve.

    Attributes:
      parameters: A tuple containing the parameters to fit with
    """

    _NUM_PARAMS = 4
    _EQ_STRING = "a + b /(1 + e^(d * x-c) )"
    _LATEX_STRING = r"a + \frac{b}{1 + e^{(dx-c)}}"

    @classmethod
    def equation(cls, x, a, b, c, d):
        """Returns a equation to be used for fitting"""
        return a + b / (1 + np.exp(d * np.array(x) - c))

    @classmethod
    def inverse(cls, y, a, b, c, d):
        """Returns a equation to be used for fitting"""
        return (np.log(b / (np.array(y) - a) - 1) + c) / d

    @classmethod
    def grad(cls, x, a, b, c, d):
        """Returns the gradient of the log equation"""
        del a

        x = np.array(x)

        exp = np.exp(d * np.array(x) - c)

        del_a = np.ones(x.shape)
        del_b = 1 / (1 + exp)
        del_c = (b * exp) / ((1 + exp) ** 2)
        del_d = (b * x * exp) / ((1 + exp) ** 2)
        return np.stack([del_a, del_b, del_c, del_d]).reshape(cls._NUM_PARAMS, 1, -1)

    @classmethod
    def get_bounds(cls, x, y):
        """Defines bounds to try to fit the curve fitting"""
        del x, y
        return (-np.inf, np.inf)

    @classmethod
    def get_initial_values(cls, x, y):
        """Defines initial value to test best guess"""
        x = np.array(x)
        y = np.array(y)

        sign = np.sign(x[np.argmax(y)] - x[np.argmin(y)])
        y_max = np.max(y)
        y_min = np.min(y)
        if sign >= 0:
            a = 0.7 * y_min
        else:
            a = 1.3 * y_max
        b = sign * 2 * (y_max - y_min)
        c = -1
        d = -1 / np.max(x)
        return (a, b, c, d)
