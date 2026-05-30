import json
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from app.models import Product, PriceSnapshot
from app.serializers import product_to_dict, snapshot_to_dict, product_detail_to_dict


def _get_product_or_404(product_id):
    try:
        return Product.objects.get(pk=product_id), None
    except Product.DoesNotExist:
        return None, JsonResponse({"error": "Not found"}, status=404)


def dashboard(request):
    return render(request, "dashboard.html")


def add_page(request):
    return render(request, "add.html")


@csrf_exempt
def products_view(request):
    if request.method == "GET":
        return _list_products(request)
    elif request.method == "POST":
        return _create_product(request)
    return JsonResponse({"error": "Method not allowed"}, status=405)


def _list_products(request):
    products = Product.objects.all()[:100]
    return JsonResponse([product_to_dict(p) for p in products], safe=False)


def _create_product(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    url = data.get("url", "").strip()
    if not url:
        return JsonResponse({"error": "url is required"}, status=400)

    product = Product.objects.create(url=url)
    return JsonResponse(product_to_dict(product), status=201)


@csrf_exempt
def product_detail_view(request, product_id):
    if request.method == "GET":
        return _get_product(request, product_id)
    elif request.method == "PATCH":
        return _update_product(request, product_id)
    elif request.method == "DELETE":
        return _delete_product(request, product_id)
    return JsonResponse({"error": "Method not allowed"}, status=405)


def _get_product(request, product_id):
    product, err = _get_product_or_404(product_id)
    if err:
        return err
    return JsonResponse(product_detail_to_dict(product))


def _update_product(request, product_id):
    product, err = _get_product_or_404(product_id)
    if err:
        return err
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if "title" in data:
        product.title = data["title"]
    if "image_url" in data:
        product.image_url = data["image_url"]
    if "rating" in data:
        product.rating = data["rating"]
    product.save()

    return JsonResponse(product_to_dict(product))


def _delete_product(request, product_id):
    product, err = _get_product_or_404(product_id)
    if err:
        return err
    product.delete()
    return HttpResponse(status=204)


@csrf_exempt
@require_http_methods(["POST"])
def create_snapshot(request):
    product_id = request.GET.get("product_id")
    price = request.GET.get("price")
    currency = request.GET.get("currency", "USD")

    if not product_id or price is None:
        return JsonResponse({"error": "product_id and price are required"}, status=400)

    try:
        product_id = int(product_id)
        price = float(price)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid product_id or price"}, status=400)

    product, err = _get_product_or_404(product_id)
    if err:
        return err
    snapshot = PriceSnapshot.objects.create(
        product=product, price=price, currency=currency
    )
    return JsonResponse(snapshot_to_dict(snapshot), status=201)
