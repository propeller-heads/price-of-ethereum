from __future__ import annotations

import inspect
from importlib.metadata import version

import price_of_ethereum


def test_version_exposed() -> None:
    assert price_of_ethereum.__version__ == version("price-of-ethereum")


def test_every_exported_name_resolves() -> None:
    missing = [name for name in price_of_ethereum.__all__ if not hasattr(price_of_ethereum, name)]
    assert not missing, f"__all__ names nothing can import: {missing}"


def test_no_public_class_or_function_is_left_out_of_all() -> None:
    # The package docstring promises `__all__` is the whole public API, so a
    # class or function pulled into this namespace and left out of the list is
    # reachable but undocumented — and stays that way silently.
    exported = set(price_of_ethereum.__all__)
    unlisted = sorted(
        name
        for name, value in vars(price_of_ethereum).items()
        if not name.startswith("_")
        and name not in exported
        and (inspect.isclass(value) or inspect.isfunction(value))
        and getattr(value, "__module__", "").startswith("price_of_ethereum")
    )
    assert not unlisted, f"imported into the package but absent from __all__: {unlisted}"
