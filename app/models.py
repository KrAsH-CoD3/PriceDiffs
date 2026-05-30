from django.db import models


class Product(models.Model):
    url = models.URLField()
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
