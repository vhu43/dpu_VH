"""A modeule for utility functions for calculations related to evolver inputs"""

from typing import Any

import numpy as np
from scipy import special


def causal_differentiator_coefs(n: int) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Returns coefficients for a noise-robust causul differentiator for N points.

  Implements a causal differentiator by returning a tuple of of coefficients to
  use for causal noise-robust numerical differneitation that uses N points.

  See the following for reference
  http://www.holoborodko.com/pavel/wp-content/uploads/\
    OneSidedNoiseRobustDifferentiators.pdf
  """
    coefs = []
    odd = bool(n % 2)

    m = (n - 3) // 2
    binom_length = m + 3
    coefs = np.zeros(n)

    binom = np.zeros(binom_length)
    for i in range(binom_length):
        binom[i] = special.comb(2 * m, m - i, exact=True)

    diff = binom[:-2] - binom[2:]
    coefs[: m * 2 + 3] = np.hstack((-np.flip(diff, 0), [0], diff))

    if not odd:
        coefs = coefs + np.roll(coefs, 1)
    return coefs


def causal_smoother_coefs(n: int) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Returns coefficients for a noise-robust smoother for N points.

  Implements a causal smoother by returning a tuple of of coefficients to
  use for causal noise-robust numerical differneitation that uses N points. Use
  this to avoid ripples due to sav-golay having imperfect high-freq rejection

  See the following for reference
  http://www.holoborodko.com/pavel/numerical-methods/\
    noise-robust-smoothing-filter/
  """
    coefs = np.zeros(n)
    odd = bool(n % 2)

    m = (n - 1) // 2

    for k in range(0, 2 * m + 1):
        A = 3 * m - 1 - 2 * (k - m) ** 2
        coefs[k] = A * special.comb(2 * m, k, exact=True) / (2 * m - 1)

    if not odd:
        coefs = coefs + np.roll(coefs, 1)
    return coefs
