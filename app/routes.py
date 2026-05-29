from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import Product, PriceSnapshot
from app.schemas import ProductCreate, ProductUpdate, ProductOut, PriceSnapshotOut, ProductDetail

router = APIRouter()


@router.get("/api/products", response_model=list[ProductOut])
async def list_products(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product).order_by(Product.created_at.desc()))
    return result.scalars().all()


@router.post("/api/products", response_model=ProductOut, status_code=201)
async def create_product(data: ProductCreate, db: AsyncSession = Depends(get_db)):
    product = Product(url=data.url)
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


@router.get("/api/products/{product_id}", response_model=ProductDetail)
async def get_product(product_id: int, db: AsyncSession = Depends(get_db)):
    product = await db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")

    result = await db.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.product_id == product_id)
        .order_by(PriceSnapshot.scraped_at.desc())
    )
    snapshots = result.scalars().all()

    detail = ProductDetail(
        product=product,
        snapshots=[PriceSnapshotOut.model_validate(s) for s in snapshots],
    )

    if snapshots:
        detail.current_price = snapshots[0].price
        detail.lowest_price = min(s.price for s in snapshots)
        detail.highest_price = max(s.price for s in snapshots)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [s for s in snapshots if s.scraped_at.replace(tzinfo=timezone.utc) >= cutoff]
        if len(recent) >= 2:
            detail.price_change_24h = round(recent[-1].price - recent[0].price, 2)

    return detail


@router.patch("/api/products/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: int, data: ProductUpdate, db: AsyncSession = Depends(get_db)
):
    product = await db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    if data.title is not None:
        product.title = data.title
    if data.image_url is not None:
        product.image_url = data.image_url
    if data.rating is not None:
        product.rating = data.rating
    await db.commit()
    await db.refresh(product)
    return product


@router.delete("/api/products/{product_id}", status_code=204)
async def delete_product(product_id: int, db: AsyncSession = Depends(get_db)):
    product = await db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    await db.delete(product)
    await db.commit()


@router.post("/api/snapshots", response_model=PriceSnapshotOut, status_code=201)
async def create_snapshot(
    product_id: int = Query(...),
    price: float = Query(...),
    currency: str = Query(default="USD"),
    db: AsyncSession = Depends(get_db),
):
    product = await db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    snapshot = PriceSnapshot(product_id=product_id, price=price, currency=currency)
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot
