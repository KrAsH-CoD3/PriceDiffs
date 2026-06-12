from django.conf import settings
from django.db import models


class Product(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="products",
        null=True,
        blank=True,
    )
    url = models.URLField(max_length=2000)
    title = models.CharField(max_length=500, default="")
    image_url = models.URLField(max_length=2000, default="")
    rating = models.CharField(max_length=50, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class PriceSnapshot(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="snapshots"
    )
    price = models.FloatField()
    currency = models.CharField(max_length=10, default="USD")
    scraped_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-scraped_at"]


class ScrapeEvent(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="events"
    )
    event_type = models.CharField(max_length=50)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["product", "created_at"]),
        ]
