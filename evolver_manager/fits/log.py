"""Implements a logirthmic fit for evolver

Logirthmic fit for evolver that extends fit class, implementing a get_val method
to return a value from a fit, a initial value method for numerically stable
Logrithmic fitting and for printing of latex string for matplotlib.
"""

import numpy as np

from . import fit


class Log(fit.Fit):
    """Log extending Fit class to implement logrithmic curve fitting operations

    Logrithmic curve stores parametes for a 4-paramter model for translated
    Logrithmic curve.

    Attributes:
      parameters: A tuple containing the parameters to fit with
    """

    _NUM_PARAMS = 3
    _EQ_STRING = "a * log (b * (x-c) )"
    _LATEX_STRING = r"a\ log(b(x-c))"

    @classmethod
    def equation(cls, x, a, b, c):
        """Returns the result of the equation of the model"""
        return a * np.log(b * (np.array(x) - c))

    @classmethod
    def inverse(cls, y, a, b, c):
        """Returns the result of the inverse equation of the model"""
        return np.exp(np.array(y) / a) / b + c

    @classmethod
    def grad(cls, x, a, b, c):
        """Returns the gradient of the log equation"""
        x = np.array(x)

        del_a = np.log(b * (x - c))
        del_b = np.ones(x.shape) * a / b
        del_c = a / (c - x)
        return np.stack([del_a, del_b, del_c]).reshape(cls._NUM_PARAMS, 1, -1)

    @classmethod
    def get_bounds(cls, x, y):
        """Defines bounds to try to fit the curve fitting"""
        # Figure out if it is increasing or decreasing. If it is increasing, make d
        # positive and c less than the min + some wiggle room. Else, make d negative
        # and c greater than the max of x.
        x = np.array(x)
        y = np.array(y)

        y_argmax = np.argmax(y)
        y_argmin = np.argmin(y)
        sign = np.sign(x[y_argmax] - x[y_argmin])
        if sign <= 0:  # Decreasing. Negative b, c > xmax
            return ([-np.inf, -np.inf, np.max(x)], [np.inf, 0, np.inf])
        else:  # Increasing. Positive b, c < xmin
            return ([-np.inf, 0, -np.inf], [np.inf, np.inf, np.min(x)])

    @classmethod
    def get_initial_values(cls, x, y):
        """Defines initial value to test best guess"""
        x = np.array(x)
        y = np.array(y)

        sign = np.sign(x[np.argmax(y)] - x[np.argmin(y)])
        y_max = np.max(y)
        y_min = np.min(y)
        a = (y_max - y_min) / 2
        b = sign
        if sign <= 0:
            c = np.max(x) * 1.1
        else:
            c = np.min(x) * 0.9
        return (a, b, c)
