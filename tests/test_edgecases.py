"""Invalid action payloads must produce client errors rather than server errors."""
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient
from crm.models import Customer, User

pytestmark = pytest.mark.django_db


@pytest.fixture
def administrator():
    cache.clear()
    user = User.objects.create_user(username='edge-admin', password='StrongGel2026!', role='superadmin')
    client = APIClient()
    client.force_authenticate(user)
    client.raise_request_exception = False
    return client


def test_login_rejects_non_object_json():
    cache.clear()
    client = APIClient()
    client.raise_request_exception = False
    response = client.post('/api/login/', ['unexpected'], format='json')
    assert response.status_code == 400


def test_assignment_rejects_non_scalar_manager_id(administrator):
    customer = Customer.objects.create(name='Queue', phone='+79998882211')
    response = administrator.post(f'/api/customers/{customer.pk}/assign/', {'manager_id': []}, format='json')
    assert response.status_code == 400


def test_restore_rejects_non_scalar_password(administrator):
    response = administrator.post('/api/backups/restore/', {
        'confirmation': 'ВОССТАНОВИТЬ', 'password': ['secret'], 'backup_id': 1,
    }, format='json')
    assert response.status_code == 400
