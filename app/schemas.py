from datetime import datetime
from pydantic import BaseModel, HttpUrl


class ProductCreate(BaseModel):
    url: str


class ProductUpdate(BaseModel):
    title: str | None = None
    image_url: str | None = None
    rating: str | None = None


class ProductOut(BaseModel):
    id: int
    url: str
    title: str
    image_url: str
    rating: str
    created_at: datetime

    model_config = {"from_attributes": True}


class PriceSnapshotOut(BaseModel):
    id: int
    product_id: int
    price: float
    currency: str
    scraped_at: datetime

    model_config = {"from_attributes": True}


class ProductDetail(BaseModel):
    product: ProductOut
    snapshots: list[PriceSnapshotOut]
    current_price: float | None = None
    lowest_price: float | None = None
    highest_price: float | None = None
    price_change_24h: float | None = None
