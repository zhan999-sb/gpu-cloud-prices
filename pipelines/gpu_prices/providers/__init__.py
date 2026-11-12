"""Provider contract; only Azure is approved and registered in this MVP."""

from typing import Protocol
from ..schema import Offer


class PriceProvider(Protocol):
    name: str
    pages_fetched: int
    complete: bool

    def collect(self, *, limit_pages: int | None = None) -> list[Offer]: ...
