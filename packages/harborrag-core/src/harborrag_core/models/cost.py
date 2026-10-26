"""Transparent cost estimates for answer-generation model calls."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel


class ModelCost(StrictModel):
    """Known generation spend, with missing prices explicitly represented.

    ``amount_usd`` is the sum of priced calls, not a complete request bill.
    Retrieval, memory maintenance, and background title generation are outside
    this scope. A partial aggregate retains its known subtotal and reports
    ``complete=False``; an entirely unpriced aggregate has no amount.
    """

    amount_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: Literal["USD"] = "USD"
    status: Literal["estimated", "unavailable"] = "unavailable"
    complete: bool = False
    scope: Literal["answer_generation"] = "answer_generation"
    model_calls: int = Field(default=0, ge=0)
    priced_model_calls: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def consistent_coverage(self) -> Self:
        """Reject summaries that could misrepresent unknown spend as free."""

        priced = self.priced_model_calls
        if priced > self.model_calls:
            raise ValueError("priced_model_calls cannot exceed model_calls")
        if (self.amount_usd is not None) != (priced > 0):
            raise ValueError("amount_usd requires at least one priced model call")
        if self.complete != (self.model_calls > 0 and priced == self.model_calls):
            raise ValueError("complete must reflect model-call pricing coverage")
        if self.status != ("estimated" if priced else "unavailable"):
            raise ValueError("status must reflect available pricing")
        return self

    def add_call(self, amount_usd: float | None) -> ModelCost:
        """Return a new aggregate including one completed model call."""

        priced = self.priced_model_calls + int(amount_usd is not None)
        calls = self.model_calls + 1
        amount = self.amount_usd
        if amount_usd is not None:
            amount = (amount or 0.0) + amount_usd
        return ModelCost(
            amount_usd=amount,
            status="estimated" if priced else "unavailable",
            complete=priced == calls,
            model_calls=calls,
            priced_model_calls=priced,
        )


__all__ = ["ModelCost"]
