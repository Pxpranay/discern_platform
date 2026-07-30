from django.contrib.auth import views as auth_views
from django.urls import path

from . import views, views_admin, views_dash, views_proc, views_works

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

    path("procurement/", views_proc.procurement_home, name="procurement_home"),
    path("procurement/requests/<int:pk>/", views_proc.request_detail, name="request_detail"),
    path("procurement/rfq/<int:pk>/", views_proc.rfq_detail, name="rfq_detail"),
    path("procurement/orders/<int:pk>/", views_proc.po_detail, name="po_detail"),
    path("receipts/", views_proc.receipts, name="receipts"),

    path("portfolio/", views_dash.portfolio, name="portfolio"),
    path("projects/<int:pk>/dashboard/", views_dash.pm_dashboard, name="pm_dashboard"),
    path("procurement/dashboard/", views_dash.purchase_dashboard, name="purchase_dashboard"),
    path("expenses/<int:pk>/sheet/", views_dash.expense_sheet, name="expense_sheet"),

    path("m/", views_dash.m_home, name="m_home"),
    path("m/receive/<int:pk>/", views_dash.m_receive, name="m_receive"),
    path("m/verify/<int:pk>/", views_dash.m_verify, name="m_verify"),
    path("m/progress/<int:pk>/", views_dash.m_progress, name="m_progress"),
    path("m/expense/", views_dash.m_expense, name="m_expense"),

    path("works/", views_works.works, name="works"),
    path("expenses/", views_works.expenses, name="expenses"),
    path("excess-stock/", views_works.excess_stock, name="excess_stock"),

    path("admin-panel/", views_admin.admin_home, name="admin_home"),
    path("admin-panel/roles/", views_admin.role_list, name="role_list"),
    path("admin-panel/roles/<int:pk>/", views_admin.role_detail, name="role_detail"),
    path("admin-panel/users/", views_admin.user_list, name="user_list"),
    path("admin-panel/users/<int:pk>/", views_admin.user_detail, name="user_detail"),
]
