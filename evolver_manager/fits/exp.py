"""Implements a Exponential fit for evolver

Exponential fit for evolver that extends fit class, implementing a get_val method
to return a value from a fit, a initial value method for numerically stable
Exponential fitting and for printing of latex string for matplotlib.
"""
from . import fit
import numpy as np

class Exp(fit.Fit):
  """Exp extending Fit class to implement Exponential curve fitting operations

  Exponential curve stores parametes for a 4-paramter model for translated
  Exponential curve.

  Attributes:
    parameters: A tuple containing the parameters to fit with
  """

  _NUM_PARAMS = 2
  _EQ_STRING = "a * e^(b * x)"
  _LATEX_STRING = r"a e^{bx}"

  @classmethod
  def equation(cls, x, a, b):
    """Returns a equation to be used for fitting"""
    return a * np.exp(b*x)

  @classmethod
  def inverse(cls, y, a, b):
    """Returns a equation to be used for fitting"""
    return np.log(y / a)/b

  @classmethod
  def grad(cls, x, a, b):
    """Returns the gradient of the log equation"""
    x = np.array(x)

    exp = a * np.exp(b * np.array(x))

    del_a = np.ones(x.shape)
    del_b = exp * b
    return (np.stack([del_a, del_b])
      .reshape(cls._NUM_PARAMS, 1, -1))

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

    a = np.min(y)
    b = sign
    return((a, b))
