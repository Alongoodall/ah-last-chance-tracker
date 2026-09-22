"""Pydantic models for AH API responses.

These models mirror the structure returned by the AH GraphQL API.
We preserve the API's naming/nesting rather than prematurely normalising
so that no information is silently lost.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Store models (from storesSearch GraphQL query)
# ---------------------------------------------------------------------------

class AHStoreAddress(BaseModel):
    """Address of an Albert Heijn store."""

    street: str
    house_number: str = Field(alias="houseNumber")
    house_number_extra: str | None = Field(default=None, alias="houseNumberExtra")
    postal_code: str = Field(alias="postalCode")
    city: str


class AHStore(BaseModel):
    """An Albert Heijn store location."""

    id: int
    name: str
    store_type: str = Field(alias="storeType")
    address: AHStoreAddress

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Bargain models (from bargainItems GraphQL query)
# ---------------------------------------------------------------------------

class AHBargainProduct(BaseModel):
    """Basic product information embedded in a bargain item."""

    id: int
    title: str
    brand: str = ""
    sales_unit_size: str = Field(default="", alias="salesUnitSize")


class AHBargainMarkdown(BaseModel):
    """Markdown (discount) information for a bargain item."""

    markdown_type: str = Field(default="", alias="markdownType")
    markdown_expiration_date: str = Field(default="", alias="markdownExpirationDate")
    markdown_percentage: float = Field(default=0.0, alias="markdownPercentage")


class AHBargainPrice(BaseModel):
    """Pricing information for a bargain item.

    Prices are returned as strings by the API (e.g. "3.29").
    We keep them as strings here to avoid floating-point rounding issues
    and convert when needed.
    """

    price_was: str = Field(default="", alias="priceWas")
    price_now: str = Field(default="", alias="priceNow")


class AHBargainItem(BaseModel):
    """A single bargain (Laatste Kans) item as returned by the API.

    This is the raw GraphQL response shape — no fields are discarded.
    """

    product: AHBargainProduct
    category_title: str = Field(default="", alias="categoryTitle")
    markdown: AHBargainMarkdown
    stock: int = 0
    bargain_price: AHBargainPrice = Field(alias="bargainPrice")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# GraphQL envelope types
# ---------------------------------------------------------------------------

class GraphQLError(BaseModel):
    """A single error from a GraphQL response."""

    message: str
    path: list[str] = []


class StoresSearchResult(BaseModel):
    """Wrapper for the storesSearch GraphQL response."""

    stores_search: StoresSearchInner = Field(alias="storesSearch")


class StoresSearchInner(BaseModel):
    result: list[AHStore] = []


class BargainItemsResult(BaseModel):
    """Wrapper for the bargainItems GraphQL response."""

    bargain_items: list[AHBargainItem] = Field(default=[], alias="bargainItems")


# Rebuild models that reference forward-declared types
StoresSearchResult.model_rebuild()
