import json
import re
from urllib.parse import urlparse, urlunparse
from django.contrib.auth import authenticate, login
from django.contrib.auth.models import User
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from app.models import Product, PriceSnapshot, ScrapeEvent
from app.serializers import (
    RegisterSerializer,
    UserSerializer,
    ProductSerializer,
    ProductDetailSerializer,
    PriceSnapshotSerializer,
)
from app.tasks import scrape_product


_TRACKING_PARAMS = re.compile(
    r"^(ref|_encoding|content-id|dib|dib_tag|pd_rd_r|pd_rd_w|pd_rd_wg|qid|sr|th|spIA|psc|smid|pf_rd_.*)$",
    re.I,
)


def _clean_url(raw: str) -> str:
    raw = raw.strip().strip('"').strip("'")
    parsed = urlparse(raw)
    if parsed.query:
        clean_qs = "&".join(
            p for p in parsed.query.split("&")
            if not _TRACKING_PARAMS.match(p.split("=")[0])
        )
        parsed = parsed._replace(query=clean_qs)
    return urlunparse(parsed)


def _jwt_response(user):
    refresh = RefreshToken.for_user(user)
    return {
        "user": UserSerializer(user).data,
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }


# ── HTML pages ─────────────────────────────────────────────────────────

def dashboard(request):
    return render(request, "dashboard.html")


def add_page(request):
    return render(request, "add.html")


def product_page(request, product_id):
    if not request.user.is_authenticated:
        raise Http404
    try:
        Product.objects.get(pk=product_id, user=request.user)
    except Product.DoesNotExist:
        raise Http404
    return render(request, "product.html", {"product_id": product_id})


def login_page(request):
    return render(request, "login.html")


def register_page(request):
    return render(request, "register.html")


# ── Auth API ────────────────────────────────────────────────────────────

@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def register_view(request):
    serializer = RegisterSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    user = serializer.save()
    login(request, user)
    return Response(_jwt_response(user), status=status.HTTP_201_CREATED)


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request):
    username = request.data.get("username", "")
    password = request.data.get("password", "")
    user = authenticate(request, username=username, password=password)
    if user is None:
        return Response({"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)
    login(request, user)
    return Response(_jwt_response(user))


@csrf_exempt
@api_view(["GET"])
def me_view(request):
    return Response(UserSerializer(request.user).data)


# ── Product API ─────────────────────────────────────────────────────────

@csrf_exempt
@api_view(["GET", "POST"])
def products_view(request):
    if request.method == "GET":
        products = Product.objects.filter(user=request.user).prefetch_related("snapshots")
        serializer = ProductSerializer(products, many=True)
        return Response(serializer.data)

    data = request.data.copy()
    if "url" in data:
        data["url"] = _clean_url(data["url"])
        data["title"] = urlparse(data["url"]).hostname or ""
    serializer = ProductSerializer(data=data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    product = serializer.save(user=request.user)
    scrape_product.delay(product.id)
    data = ProductSerializer(product).data
    return Response(data, status=status.HTTP_201_CREATED)


@csrf_exempt
@api_view(["GET", "PATCH", "DELETE"])
def product_detail_view(request, product_id):
    try:
        product = Product.objects.get(pk=product_id, user=request.user)
    except Product.DoesNotExist:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        serializer = ProductDetailSerializer(product)
        return Response(serializer.data)

    if request.method == "PATCH":
        serializer = ProductSerializer(product, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(ProductSerializer(product).data)

    product.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@csrf_exempt
@api_view(["POST"])
def create_snapshot(request):
    product_id = request.data.get("product_id")
    price = request.data.get("price")
    currency = request.data.get("currency", "USD")

    if not product_id or price is None:
        return Response(
            {"error": "product_id and price are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        price = float(price)
    except (ValueError, TypeError):
        return Response({"error": "Invalid price"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        product = Product.objects.get(pk=product_id, user=request.user)
    except Product.DoesNotExist:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

    snapshot = PriceSnapshot.objects.create(
        product=product, price=price, currency=currency
    )
    serializer = PriceSnapshotSerializer(snapshot)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@csrf_exempt
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def poll_events(request):
    last_id = int(request.GET.get("last_id", 0))
    events = list(
        ScrapeEvent.objects.filter(
            product__user=request.user,
            id__gt=last_id,
        ).select_related("product").order_by("id")[:50]
    )
    result = []
    for event in events:
        result.append({
            "id": event.id,
            "event_type": event.event_type,
            "product_id": event.product_id,
            "product_title": event.product.title,
            "product_image_url": event.product.image_url,
            "product_rating": event.product.rating,
            **event.data,
        })
    return Response(result)


@csrf_exempt
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def scrape_events_max_id(request):
    max_id = ScrapeEvent.objects.filter(product__user=request.user).order_by("-id").values_list("id", flat=True).first()
    return Response(max_id or 0)
