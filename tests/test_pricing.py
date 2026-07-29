"""Unit tests for the pure pricing math."""

from __future__ import annotations

import pytest

from price_of_ethereum.pricing import (
    ROBUST_MID_MIN_DEPTH,
    choose_robust_mid,
    derive_price_impact_bps,
    execution_price,
    impact_pct,
    robust_mid_from_sides,
    robust_mid_probe_depths,
)


class TestExecutionPrice:
    def test_buy_is_notional_over_token_out(self) -> None:
        price = execution_price(
            side="buy",
            amount_out=str(4 * 10**17),  # 0.4 token out
            notional=1000.0,
            token_decimals=18,
            numeraire_decimals=6,
        )
        assert price == 2500.0

    def test_buy_zero_out_is_none(self) -> None:
        price = execution_price(
            side="buy", amount_out="0", notional=1000.0, token_decimals=18, numeraire_decimals=6
        )
        assert price is None

    def test_sell_is_numeraire_out_over_token_in(self) -> None:
        # notional 1000 at spot 2500 -> 0.4 token in; 990 numeraire out -> 2475.
        price = execution_price(
            side="sell",
            amount_out=str(990 * 10**6),
            notional=1000.0,
            token_decimals=18,
            numeraire_decimals=6,
            spot=2500.0,
        )
        assert price == pytest.approx(2475.0)

    def test_sell_requires_spot(self) -> None:
        with pytest.raises(ValueError, match="spot"):
            execution_price(
                side="sell",
                amount_out="1",
                notional=1000.0,
                token_decimals=18,
                numeraire_decimals=6,
            )


class TestImpactPct:
    def test_buy_above_reference_is_positive(self) -> None:
        assert impact_pct(2525.0, 2500.0, "buy") == pytest.approx(1.0)

    def test_sell_below_reference_is_positive(self) -> None:
        assert impact_pct(2475.0, 2500.0, "sell") == pytest.approx(1.0)

    def test_a_side_that_beats_the_reference_is_negative(self) -> None:
        assert impact_pct(2475.0, 2500.0, "buy") == pytest.approx(-1.0)
        assert impact_pct(2525.0, 2500.0, "sell") == pytest.approx(-1.0)


class TestDerivePriceImpactBps:
    def test_costly_side_is_positive_and_rounded_to_one_decimal(self) -> None:
        assert derive_price_impact_bps(2525.0, 2500.0, "buy") == 100.0
        assert derive_price_impact_bps(2475.0, 2500.0, "sell") == 100.0
        assert derive_price_impact_bps(2500.03, 2500.0, "buy") == 0.1

    def test_is_impact_pct_in_a_hundredth_of_the_unit(self) -> None:
        for price, side in ((2525.0, "buy"), (2475.0, "sell"), (2475.0, "buy")):
            bps = derive_price_impact_bps(price, 2500.0, side)
            assert bps == pytest.approx(impact_pct(price, 2500.0, side) * 100.0)

    def test_none_on_missing_inputs(self) -> None:
        assert derive_price_impact_bps(None, 2500.0, "buy") is None
        assert derive_price_impact_bps(2500.0, None, "buy") is None
        assert derive_price_impact_bps(0.0, 2500.0, "buy") is None


class TestChooseRobustMid:
    def test_empty_is_none(self) -> None:
        assert choose_robust_mid([]) is None

    def test_median_of_band_pairs(self) -> None:
        pairs = [(3000.0, 2501.0), (5000.0, 2502.0), (9000.0, 2503.0)]
        assert choose_robust_mid(pairs) == (2502.0, 5000.0)

    def test_out_of_band_falls_back_to_log_nearest(self) -> None:
        # Only 2 in band -> fall back to the 5 pairs log-nearest to $5000,
        # which pulls in out-of-band depths.
        pairs = [
            (100.0, 2490.0),
            (400.0, 2492.0),
            (1_200.0, 2494.0),
            (3_000.0, 2500.0),
            (9_000.0, 2502.0),
            (40_000.0, 2510.0),
            (200_000.0, 2520.0),
        ]
        result = choose_robust_mid(pairs)
        assert result is not None
        mid, median_depth = result
        # Log-nearest five to 5000: 1200, 3000, 9000, 40_000, 400 -> median 2500.
        assert mid == 2500.0
        assert median_depth == 3000.0

    def test_non_finite_pairs_dropped(self) -> None:
        assert choose_robust_mid([(float("nan"), 2500.0), (5000.0, float("inf"))]) is None

    def test_a_scaled_band_selects_scaled_depths(self) -> None:
        # A numeraire worth $2,500 puts the same dollar band at 1-4 units, where
        # the default band would find nothing.
        pairs = [(1.2, 0.98), (2.0, 1.0), (3.5, 1.02)]
        assert choose_robust_mid(pairs, band_min=1.0, band_max=4.0) == (1.0, 2.0)

    def test_band_edges_are_inclusive(self) -> None:
        # Three pairs sit strictly inside, so the under-filled-band fallback does
        # not fire and the pair sitting exactly on band_max decides the median.
        pairs = [(1.5, 10.0), (2.0, 20.0), (2.5, 30.0), (4.0, 100.0)]
        assert choose_robust_mid(pairs, band_min=1.0, band_max=4.0) == (25.0, 2.0)


class TestRobustMidFromSides:
    def test_pairs_by_rounded_notional(self) -> None:
        buy = [(2999.999, 2510.0), (5000.0, 2512.0), (9000.0, 2514.0)]
        sell = [(3000.001, 2490.0), (5000.0, 2492.0), (9000.0, 2494.0)]
        # Six figures absorbs the float gap between the two sides, so 2999.999
        # and 3000.001 land on one key and pair up.
        assert robust_mid_from_sides(buy, sell) == (2502.0, 5000.0)

    def test_unmatched_sell_rungs_skipped(self) -> None:
        buy = [(3000.0, 2510.0)]
        sell = [(3000.0, 2490.0), (7000.0, 2480.0)]
        assert robust_mid_from_sides(buy, sell) == (2500.0, 3000.0)

    def test_no_pairs_is_none(self) -> None:
        assert robust_mid_from_sides([(3000.0, 2510.0)], [(7000.0, 2490.0)]) is None


class TestRobustMidProbeDepths:
    def test_shallow_max_collapses_to_min_depth(self) -> None:
        assert robust_mid_probe_depths(1000.0) == [ROBUST_MID_MIN_DEPTH]

    def test_deep_max_clamps_to_band(self) -> None:
        depths = robust_mid_probe_depths(500_000.0)
        assert len(depths) == 5
        assert depths[0] == pytest.approx(2500.0)
        assert depths[-1] == pytest.approx(10_000.0)
        assert depths == sorted(depths)

    def test_a_scaled_band_spans_scaled_depths(self) -> None:
        # The probe span follows the band it was given, not the dollar-shaped
        # default, so a cheap numeraire is not truncated to the old ceiling.
        depths = robust_mid_probe_depths(1_000_000.0, band_min=250_000.0, band_max=1_000_000.0)
        assert len(depths) == 5
        assert depths[0] == pytest.approx(250_000.0)
        assert depths[-1] == pytest.approx(1_000_000.0)
