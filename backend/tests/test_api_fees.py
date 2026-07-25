"""Tests for the fee-quote endpoint.

Regression coverage for a real break: when fee inputs migrated from integer
cents to dollar strings, this endpoint kept passing cents and every call
500'd. The engine's validation caught it, but only at runtime — nothing
tested the API layer. Now something does.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routes.health import fee_quote


class TestFeeQuote:
    async def test_prices_a_normal_market(self) -> None:
        result = await fee_quote(price_dollars="0.5000", contracts="100")
        assert result["fee_cents"] == 175
        # Fixed 4dp so the UI has a predictable string to format.
        assert result["fee_per_contract_cents"] == "1.7500"

    async def test_single_contract_rounds_up(self) -> None:
        result = await fee_quote(price_dollars="0.5000", contracts="1")
        assert result["fee_cents"] == 2

    async def test_accepts_fractional_contracts(self) -> None:
        result = await fee_quote(price_dollars="0.5000", contracts="2.50")
        assert result["fee_cents"] == 5

    async def test_maker_is_cheaper_than_taker(self) -> None:
        taker = await fee_quote(price_dollars="0.5000", contracts="1000")
        maker = await fee_quote(
            price_dollars="0.5000", contracts="1000", is_taker=False
        )
        assert maker["fee_cents"] < taker["fee_cents"]

    async def test_unverified_category_returns_409_not_a_number(self) -> None:
        """Fail closed at the API boundary, not just in the engine."""
        with pytest.raises(HTTPException) as exc:
            await fee_quote(
                price_dollars="0.5000", contracts="100", category="Crypto"
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["error"] == "unverified_fee_category"
        assert exc.value.detail["category"] == "crypto"

    async def test_category_matching_is_case_insensitive(self) -> None:
        """The catalog stores 'Crypto'; the schedule keys on 'crypto'."""
        for spelling in ("Crypto", "crypto", "CRYPTO"):
            with pytest.raises(HTTPException) as exc:
                await fee_quote(
                    price_dollars="0.5000", contracts="100", category=spelling
                )
            assert exc.value.status_code == 409

    async def test_known_category_is_priced_normally(self) -> None:
        result = await fee_quote(
            price_dollars="0.5000", contracts="100", category="Sports"
        )
        assert result["fee_cents"] == 175

    @pytest.mark.parametrize("bad_price", ["50", "0", "1", "-0.5", "nonsense"])
    async def test_bad_price_returns_400(self, bad_price: str) -> None:
        """A cents-style '50' must be rejected, not silently mispriced."""
        with pytest.raises(HTTPException) as exc:
            await fee_quote(price_dollars=bad_price, contracts="100")
        assert exc.value.status_code == 400

    async def test_zero_contracts_is_free(self) -> None:
        result = await fee_quote(price_dollars="0.5000", contracts="0")
        assert result["fee_cents"] == 0
        assert result["fee_per_contract_cents"] == "0"

    async def test_echoes_inputs_for_ui_display(self) -> None:
        result = await fee_quote(price_dollars="0.5600", contracts="100")
        assert result["price_dollars"] == "0.5600"
        assert result["contracts"] == "100"
        assert result["is_taker"] is True
