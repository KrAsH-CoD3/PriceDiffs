from django.urls import path
from rest_framework_simplejwt.views import token_refresh
from app import views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("dashboard", views.dashboard, name="dashboard"),
    path("add", views.add_page, name="add_page"),
    path("product/<int:product_id>", views.product_page, name="product_page"),
    path("login", views.login_page, name="login_page"),
    path("register", views.register_page, name="register_page"),
    path("logout", views.logout_view, name="logout"),
    path("api/auth/register", views.register_view, name="auth_register"),
    path("api/auth/login", views.login_view, name="auth_login"),
    path("api/auth/refresh", token_refresh, name="auth_refresh"),
    path("api/auth/me", views.me_view, name="auth_me"),
    path("api/products", views.products_view, name="list_products"),
    path("api/products/<int:product_id>", views.product_detail_view, name="product_detail"),
    path("api/snapshots", views.create_snapshot, name="create_snapshot"),
    path("api/events/poll", views.poll_events, name="poll_events"),
    path("api/events/max-id", views.scrape_events_max_id, name="scrape_events_max_id"),
    path("<path:path>", views.not_found),
]
