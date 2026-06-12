from datetime import datetime, timezone, timedelta
from django.contrib.auth.models import User
from rest_framework import serializers
from app.models import Product, PriceSnapshot


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"]
        read_only_fields = ["id"]


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, min_length=8)

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data.get("email", ""),
            password=validated_data["password"],
        )
        return user


class PriceSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = PriceSnapshot
        fields = ["id", "product", "price", "currency", "scraped_at"]
        read_only_fields = ["id", "scraped_at"]


class ProductSerializer(serializers.ModelSerializer):
    current_price = serializers.SerializerMethodField()
    price_change_24h = serializers.SerializerMethodField()
    snapshot_count = serializers.SerializerMethodField()
    last_currency = serializers.SerializerMethodField()
    last_scraped_at = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "url", "title", "image_url", "rating",
            "created_at", "current_price", "price_change_24h", "snapshot_count",
            "last_currency", "last_scraped_at",
        ]
        read_only_fields = ["id", "created_at"]

    def _latest_snap(self, obj):
        snaps = obj.snapshots.all()
        return snaps[0] if snaps else None

    def get_current_price(self, obj) -> float | None:
        snap = self._latest_snap(obj)
        return snap.price if snap else None

    def get_last_currency(self, obj) -> str | None:
        snap = self._latest_snap(obj)
        return snap.currency if snap else None

    def get_last_scraped_at(self, obj) -> str | None:
        snap = self._latest_snap(obj)
        return snap.scraped_at.isoformat() if snap else None

    def get_price_change_24h(self, obj) -> float | None:
        snapshots = list(obj.snapshots.all())
        if len(snapshots) < 2:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [
            s for s in snapshots
            if s.scraped_at.replace(tzinfo=timezone.utc) >= cutoff
        ]
        if len(recent) < 2:
            return None
        return round(recent[-1].price - recent[0].price, 2)

    def get_snapshot_count(self, obj) -> int:
        return len(obj.snapshots.all())


class ProductDetailSerializer(serializers.ModelSerializer):
    snapshots = PriceSnapshotSerializer(many=True, read_only=True)
    current_price = serializers.SerializerMethodField()
    lowest_price = serializers.SerializerMethodField()
    highest_price = serializers.SerializerMethodField()
    price_change_24h = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "url", "title", "image_url", "rating",
            "created_at", "snapshots",
            "current_price", "lowest_price", "highest_price", "price_change_24h",
        ]

    def get_current_price(self, obj) -> float | None:
        snap = obj.snapshots.first()
        return snap.price if snap else None

    def get_lowest_price(self, obj) -> float | None:
        snapshots = list(obj.snapshots.all())
        if not snapshots:
            return None
        return min(s.price for s in snapshots)

    def get_highest_price(self, obj) -> float | None:
        snapshots = list(obj.snapshots.all())
        if not snapshots:
            return None
        return max(s.price for s in snapshots)

    def get_price_change_24h(self, obj) -> float | None:
        snapshots = list(obj.snapshots.all())
        if len(snapshots) < 2:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [
            s for s in snapshots
            if s.scraped_at.replace(tzinfo=timezone.utc) >= cutoff
        ]
        if len(recent) < 2:
            return None
        return round(recent[-1].price - recent[0].price, 2)
