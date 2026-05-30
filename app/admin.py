from django.contrib import admin
from app.models import Product, PriceSnapshot


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["id", "title", "url", "created_at"]


@admin.register(PriceSnapshot)
class PriceSnapshotAdmin(admin.ModelAdmin):
    list_display = ["id", "product", "price", "currency", "scraped_at"]
