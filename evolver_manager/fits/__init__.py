"""Initialization of fit subpackages.

Obtains a list of valid fit types on import
"""
from pathlib import Path

# Get valid fit types as a package-wide variable
valid_fit_types = []
fit_type = None
for fit_type in Path(__file__).parent.iterdir():
  if fit_type.suffix == ".py":
    if fit_type.stem not in ("fit", "template", "__init__"):
      valid_fit_types.append(fit_type.stem)

VALID_FIT_TYPES = tuple(valid_fit_types)
del valid_fit_types
del fit_type
