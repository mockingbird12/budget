# finance/urls_pages.py
from django.urls import path
from . import views_pages

urlpatterns = [
    path('', views_pages.page_categories, name='page_categories'),
    path('incomes/', views_pages.page_incomes, name='page_incomes'),
    path('monthly/', views_pages.page_monthly, name='page_monthly'),
    path('balance/', views_pages.page_balance, name='page_balance'),
]
