"""Initialization of bioreactors subpackages.

Obtains a list of valid fit types on import
"""

import dataclasses
import importlib
import types
from pathlib import Path

from . import bioreactor

# Get valid fit types as a package-wide variable
REACTOR_SETTINGS_FIELDS = {}
valid_reactor_types = []
reactor_type = None
for reactor_type in Path(__file__).parent.iterdir():
    if reactor_type.suffix == ".py":
        if reactor_type.stem not in ("bioreactor", "template", "__init__"):
            valid_reactor_types.append(reactor_type.stem)

            # Get datafields for each valid reactor type
            reactor = importlib.import_module(
                f"evolver_manager.bioreactors.{reactor_type.stem}"
            )
            setting_class_name = reactor_type.stem.capitalize() + "Settings"
            setting_class = getattr(reactor, setting_class_name)
            REACTOR_SETTINGS_FIELDS[reactor_type.stem] = {}
            REACTOR_SETTINGS_FIELDS[reactor_type.stem]["tuple_fields"] = tuple(
                [
                    f.name
                    for f in dataclasses.fields(setting_class)
                    if isinstance(f.type, types.GenericAlias)
                    and f.type.__origin__ is tuple
                    and f.name != "base_settings"
                ]
            )

            REACTOR_SETTINGS_FIELDS[reactor_type.stem]["fields"] = tuple(
                [
                    f.name
                    for f in dataclasses.fields(setting_class)
                    if f.name != "base_settings"
                ]
            )

VALID_REACTORS = tuple(valid_reactor_types)

BASE_FIELDS = tuple([f.name for f in dataclasses.fields(bioreactor.ReactorSettings)])

BASE_TUPLE_FIELDS = tuple(
    [
        f.name
        for f in dataclasses.fields(bioreactor.ReactorSettings)
        if isinstance(f.type, types.GenericAlias) and f.type.__origin__ is tuple
    ]
)

del valid_reactor_types
del reactor_type
