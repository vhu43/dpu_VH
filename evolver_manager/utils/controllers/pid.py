"""Abstract class"""

from typing import Iterable

import controller
import numpy as np


class PID(controller.Controller):
    """Implements a PID controller with 4 parameters.

    Implements a standard  PID controler with 5 parameters, gains for the
    proportional, integral, and derivative components of the error, as well as a
    value for the exponential moving average for low-pass filtering and a value
    for the offset.

    Attributes:
      k_p: (read-only): The gain for the proportional term
      k_i: (read-only): The gain for the integral term
      k_d: (read-only): the gain for the derivative term
      alpha: (read-only): the exponential moving average parameter
      setpoint: The setpoint for the pid controller

    """

    def __init__(
        self,
        parameters: Iterable[float, float, float, float],
        setpoint: float,
        rate: float,
    ):
        super().__init__(parameters, setpoint, rate)
        self._last_err: float = 0
        self._p_term: float = 0
        self._i_term: float = 0
        self._d_term: float = 0

    @property
    def k_p(self):
        return self._parameters[0]

    @property
    def k_i(self):
        return self._parameters[1]

    @property
    def k_d(self):
        return self._parameters[2]

    @property
    def alpha(self):
        return self._parameters[3]

    def get_response(self, value: float) -> float:
        """Implements a pid response calculation.

        Implements a PID controller with windup-protected integral term and a
        low-pass exponential moving average filtered derivative term.

        arguments:
          value: the value of the input

        returns:
          A float indicating the feedback control the controller outputs
        """
        err = value - self.setpoint
        last_err = self._last_err
        self._last_err = err

        p_term = self.k_p * err
        self._p_term = p_term

        # Implement trapazoidal calculation for integral with 0-reset to prevent
        #   windup.
        if p_term == 0 or np.prod(np.sign([p_term, self._p_term])) == -1:
            self._i_term = 0
        self._i_term += self.k_i * (err + last_err) / (2 * self._rate)

        # Implmenets low-pass filtered derivative using exponential moving average
        self._d_term *= 1 - self.alpha
        self._d_term += self.alpha * (err - last_err) * self._rate

        return p_term + self._i_term + self._d_term
