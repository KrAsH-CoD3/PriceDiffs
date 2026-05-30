from django.urls import path
from app import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("add", views.add_page, name="add_page"),
    path("api/products", views.products_view, name="list_products"),
    path("api/products/<int:product_id>", views.product_detail_view, name="product_detail"),
    path("api/snapshots", views.create_snapshot, name="create_snapshot"),
]
