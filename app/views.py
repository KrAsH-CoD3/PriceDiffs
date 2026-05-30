import asyncio
from django.contrib.auth import authenticate
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from app.models import Product, PriceSnapshot
from app.serializers import (
    RegisterSerializer,
    UserSerializer,
    ProductSerializer,
    ProductDetailSerializer,
    PriceSnapshotSerializer,
)
from app.strategy import scrape_url, _close_browser


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
        products = Product.objects.filter(user=request.user)
        serializer = ProductSerializer(products, many=True)
        return Response(serializer.data)

    serializer = ProductSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    product = serializer.save(user=request.user)

    data = asyncio.run(scrape_url(product.url))
    if data:
        product.title = data.get("title", product.title)
        product.image_url = data.get("image_url", product.image_url)
        product.rating = data.get("rating", product.rating)
        product.save(update_fields=["title", "image_url", "rating"])
        PriceSnapshot.objects.create(
            product=product,
            price=data.get("price", 0),
            currency=data.get("currency", "NGN"),
        )

    asyncio.run(_close_browser())

    return Response(ProductSerializer(product).data, status=status.HTTP_201_CREATED)


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
        product = Product.objects.get(pk=product_id)
    except Product.DoesNotExist:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

    snapshot = PriceSnapshot.objects.create(
        product=product, price=price, currency=currency
    )
    serializer = PriceSnapshotSerializer(snapshot)
    return Response(serializer.data, status=status.HTTP_201_CREATED)
