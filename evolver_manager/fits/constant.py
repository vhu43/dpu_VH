"""Implements a linear fit for evolver

Constant fit for evolver that extends fit class, implementing a get_val method
to return a value from a fit, a initial value method for numerically stable
constant fitting and for printing of latex string for matplotlib.
"""
from . import fit
import numpy as np

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
  def equation(cls, x, a):
    """Returns the result of the equation of the model"""
    return a * np.ones(np.array(x).shape)

  @classmethod
  def inverse(cls, y, a):
    """Returns the result of the inverse equation of the model"""
    return np.ones(np.array(y).shape) / a

  @classmethod
  def grad(cls, x, a):
    """Returns the gradient of the log equation"""
    del a
    del_a = np.zeros(x.shape)
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
    return((0))
