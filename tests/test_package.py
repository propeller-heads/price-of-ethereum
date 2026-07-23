from __future__ import annotations

import price_of_ethereum


def test_version_exposed() -> None:
    assert price_of_ethereum.__version__ == "0.1.0"
