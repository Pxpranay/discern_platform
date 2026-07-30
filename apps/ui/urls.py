from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="ui/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),

    path("", views.dashboard, name="dashboard"),
    path("crm/", views.crm, name="crm"),
    path("orders/", views.orders, name="orders"),
    path("orders/<int:pk>/", views.order_detail, name="order_detail"),
    path("projects/", views.projects, name="projects"),
    path("projects/<int:pk>/", views.project_detail, name="project_detail"),
    path("boq/", views.boq_list, name="boq_list"),
    path("boq/<int:pk>/", views.boq_detail, name="boq_detail"),
]
