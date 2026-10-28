# finance/urls_pages.py
from django.urls import path
from . import views_pages

urlpatterns = [
    path('', views_pages.page_categories, name='page_categories'),
    path('monthly/', views_pages.page_monthly, name='page_monthly'),
]