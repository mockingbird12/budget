# finance/urls.py
from rest_framework.routers import DefaultRouter
from .views import AccountViewSet, CategoryViewSet, TransactionViewSet

router = DefaultRouter()
router.register('accounts', AccountViewSet)
router.register('categories', CategoryViewSet)
router.register('transactions', TransactionViewSet)

urlpatterns = router.urls