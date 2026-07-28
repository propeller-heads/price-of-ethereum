from __future__ import annotations

from importlib.metadata import version

import price_of_ethereum


def test_version_exposed() -> None:
    assert price_of_ethereum.__version__ == version("price-of-ethereum")
