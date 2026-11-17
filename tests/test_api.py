"""Business and authorization checks through the actual REST endpoints."""
import json
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.core.cache import cache
import pytest
from rest_framework.test import APIClient

from crm.integrations import IncomingMessage, ingest_verified_message
from crm.models import (
    AuditEvent, Customer, FinanceEntry, Material, Message, Notification, Order,
    Product, ProductionTask, Recipe, RecipeComponent, StockMovement, User,
)

pytestmark = pytest.mark.django_db
PASSWORD = 'ApiTestGel2026!'


@pytest.fixture(scope='session')
def password_hash():
    return make_password(PASSWORD)


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    cache.clear()


@pytest.fixture
def users(password_hash):
    roles = ['superadmin', 'admin', 'manager', 'colorist', 'filler', 'labeler',
             'picker', 'dispatcher', 'accountant', 'analyst']
    result = {role: User.objects.create(username=role, role=role, password=password_hash,
                                        first_name=role.title()) for role in roles}
    result['manager2'] = User.objects.create(username='manager2', role='manager', password=password_hash)
    return result


@pytest.fixture
def client_for(users):
    def factory(role):
        client = APIClient()
        client.force_login(users[role])
        return client
    return factory


@pytest.fixture
def domain(users):
    customer = Customer.objects.create(name='Наша студия', phone='8 (999) 111-22-33', manager=users['manager'])
    other = Customer.objects.create(name='Другая студия', phone='+79991112234', manager=users['manager2'])
    shared = Customer.objects.create(name='Общая очередь', phone='+79991112235')
    product = Product.objects.create(sku='GEL-001', name='База 15 мл', price='100.00', stock='100.000')
    order = Order.objects.create(customer=customer, manager=users['manager'], kind='standard',
                                 product=product, quantity=3, unit_price='100.00')
    other_order = Order.objects.create(customer=other, manager=users['manager2'], kind='standard',
                                       product=product, quantity=2, unit_price='100.00')
    raw = Material.objects.create(sku='RAW', name='База', kind='raw', unit='г', quantity='100.000')
    jar = Material.objects.create(sku='JAR', name='Баночка', kind='jar', unit='шт', quantity='1.000')
    recipe = Recipe.objects.create(name='Сиреневый')
    RecipeComponent.objects.create(recipe=recipe, material=raw, quantity='2.000')
    RecipeComponent.objects.create(recipe=recipe, material=jar, quantity='1.000')
    return {'customer': customer, 'other': other, 'shared': shared, 'product': product,
            'order': order, 'other_order': other_order, 'raw': raw, 'jar': jar, 'recipe': recipe}


def test_login_enforces_csrf_and_session_writes_require_rotated_token(users):
    client = APIClient(enforce_csrf_checks=True)
    assert client.get('/api/me/').json() == {'user': None, 'permissions': []}
    token = client.cookies['csrftoken'].value
    payload = {'username': 'manager', 'password': PASSWORD}
    assert client.post('/api/login/', payload, format='json').status_code == 403
    response = client.post('/api/login/', payload, format='json', HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 200
    assert response.json()['user']['role'] == 'manager'
    assert 'password' not in response.json()['user']
    assert 'sessionid' in client.cookies
    assert client.cookies['sessionid']['httponly']
    assert client.get('/api/me/').json()['user']['username'] == 'manager'
    new_customer = {'name': 'Контакт', 'phone': '+79990001122'}
    assert client.post('/api/customers/', new_customer, format='json').status_code == 403
    rotated = client.cookies['csrftoken'].value
    assert client.post('/api/customers/', new_customer, format='json', HTTP_X_CSRFTOKEN=rotated).status_code == 201


def test_anonymous_business_endpoints_are_private():
    client = APIClient()
    for url in ['/api/customers/', '/api/orders/', '/api/employees/', '/api/backups/', '/api/dashboard/']:
        assert client.get(url).status_code in (401, 403)


def test_limited_admin_cannot_manage_employees_audit_or_backups(client_for):
    client = client_for('admin')
    for url in ['/api/employees/', '/api/audit/', '/api/backups/']:
        assert client.get(url).status_code == 403
    assert client.post('/api/employees/', {'username': 'intruder', 'password': PASSWORD, 'role': 'superadmin'}, format='json').status_code == 403
    assert client.post('/api/backups/create/', {}, format='json').status_code == 403
    assert client.post('/api/backups/restore/', {}, format='json').status_code == 403
    assert client.get('/api/customers/').status_code == 200


def test_manager_sees_own_and_shared_contacts_but_only_own_orders(client_for, domain):
    client = client_for('manager')
    ids = {item['id'] for item in client.get('/api/customers/').json()}
    assert ids == {domain['customer'].pk, domain['shared'].pk}
    assert client.get(f"/api/customers/{domain['other'].pk}/").status_code == 404
    assert {item['id'] for item in client.get('/api/orders/').json()} == {domain['order'].pk}
    assert client.get(f"/api/orders/{domain['other_order'].pk}/").status_code == 404


def test_shared_queue_cannot_be_claimed_edited_or_messaged_by_manager(client_for, users, domain):
    client = client_for('manager')
    customer_id = domain['shared'].pk
    assert client.patch(f'/api/customers/{customer_id}/', {'name': 'Hijack'}, format='json').status_code == 403
    assert client.post(f'/api/customers/{customer_id}/assign/', {'manager_id': users['manager'].pk}, format='json').status_code == 403
    assert client.get(f'/api/customers/{customer_id}/messages/').status_code == 403
    assert client.post(f'/api/customers/{customer_id}/messages/', {'text': 'Hi'}, format='json').status_code == 403
    assert client.post('/api/orders/', {'customer': customer_id, 'kind': 'standard',
                                       'product': domain['product'].pk, 'quantity': 1, 'unit_price': '100.00'}, format='json').status_code == 400
    domain['shared'].refresh_from_db()
    assert domain['shared'].manager_id is None
    assert domain['shared'].name == 'Общая очередь'


def test_contact_creation_ignores_spoofed_owner_and_always_enters_queue(client_for, users):
    response = client_for('manager').post('/api/customers/', {
        'name': 'Новый клиент', 'phone': '+79990000001', 'manager': users['manager'].pk,
    }, format='json')
    assert response.status_code == 201
    assert response.json()['manager'] is None


def test_manual_assignment_preserves_messages_and_transfers_order_access(client_for, users, domain):
    customer_id = domain['customer'].pk
    message = Message.objects.create(customer=domain['customer'], text='История клиента', author=users['manager'])
    response = client_for('admin').post(f'/api/customers/{customer_id}/assign/',
                                       {'manager_id': users['manager2'].pk}, format='json')
    assert response.status_code == 200
    domain['order'].refresh_from_db()
    assert domain['order'].manager_id == users['manager2'].pk
    assert Message.objects.get(pk=message.pk).customer_id == customer_id
    assert client_for('manager').get(f'/api/customers/{customer_id}/messages/').status_code == 404
    response = client_for('manager2').get(f'/api/customers/{customer_id}/messages/')
    assert response.status_code == 200
    assert response.json()[0]['text'] == 'История клиента'
    assert Notification.objects.filter(user=users['manager2'], title='Вам назначен клиент').exists()


@pytest.mark.parametrize('phone', ['+7 999 111 22 33', '89991112233', '9991112233'])
def test_phone_duplicates_are_normalized_before_validation(client_for, domain, phone):
    response = client_for('admin').post('/api/customers/', {'name': 'Дубль', 'phone': phone}, format='json')
    assert response.status_code == 400
    assert Customer.objects.filter(phone='+79991112233').count() == 1


def test_contact_email_duplicates_ignore_case(client_for):
    client = client_for('admin')
    assert client.post('/api/customers/', {'name': 'Один', 'email': 'USER@example.com'}, format='json').status_code == 201
    assert client.post('/api/customers/', {'name': 'Два', 'email': 'user@EXAMPLE.com'}, format='json').status_code == 400
    assert Customer.objects.count() == 1


def test_standard_order_start_deducts_stock_once_and_replay_is_rejected(client_for, domain):
    client = client_for('manager')
    url = f"/api/orders/{domain['order'].pk}/start/"
    assert client.post(url, {}, format='json').status_code == 200
    assert client.post(url, {}, format='json').status_code == 400
    domain['product'].refresh_from_db()
    domain['order'].refresh_from_db()
    assert domain['product'].stock == Decimal('97.000')
    assert domain['order'].status == 'production'
    assert StockMovement.objects.filter(order=domain['order']).count() == 1
    assert list(domain['order'].tasks.values_list('stage', 'status')) == [('pick', 'pending'), ('ship', 'waiting')]


def test_manager_cannot_start_someone_elses_order(client_for, domain):
    response = client_for('manager').post(f"/api/orders/{domain['other_order'].pk}/start/", {}, format='json')
    assert response.status_code == 404
    domain['product'].refresh_from_db()
    assert domain['product'].stock == Decimal('100.000')


def test_custom_order_material_shortage_rolls_back_all_components_and_tasks(client_for, users, domain):
    order = Order.objects.create(customer=domain['customer'], manager=users['manager'], kind='custom',
                                 recipe=domain['recipe'], quantity=3, unit_price='200.00')
    response = client_for('manager').post(f'/api/orders/{order.pk}/start/', {}, format='json')
    assert response.status_code == 400
    domain['raw'].refresh_from_db()
    domain['jar'].refresh_from_db()
    order.refresh_from_db()
    assert domain['raw'].quantity == Decimal('100.000')
    assert domain['jar'].quantity == Decimal('1.000')
    assert order.status == 'draft'
    assert not order.tasks.exists()
    assert not StockMovement.objects.filter(order=order).exists()


def test_custom_order_runs_all_five_departments_and_consumes_recipe_once(client_for, users, domain):
    order = Order.objects.create(customer=domain['customer'], manager=users['manager'], kind='custom',
                                 recipe=domain['recipe'], quantity=1, unit_price='200.00')
    assert client_for('manager').post(f'/api/orders/{order.pk}/start/', {}, format='json').status_code == 200
    domain['raw'].refresh_from_db()
    domain['jar'].refresh_from_db()
    assert domain['raw'].quantity == Decimal('98.000')
    assert domain['jar'].quantity == Decimal('0.000')
    assert StockMovement.objects.filter(order=order).count() == 2
    sequence = [('color', 'colorist'), ('fill', 'filler'), ('label', 'labeler'),
                ('pick', 'picker'), ('ship', 'dispatcher')]
    assert list(order.tasks.values_list('stage', 'status')) == [
        (stage, 'pending' if index == 0 else 'waiting') for index, (stage, _) in enumerate(sequence)]
    for stage, role in sequence:
        task = order.tasks.get(stage=stage)
        assert task.status == 'pending'
        client = client_for(role)
        url = f'/api/tasks/{task.pk}/advance/'
        assert client.post(url, {'action': 'accept'}, format='json').status_code == 200
        assert client.post(url, {'action': 'complete', 'note': 'Контроль пройден'}, format='json').status_code == 200
    order.refresh_from_db()
    assert order.status == 'shipped'
    assert StockMovement.objects.filter(order=order).count() == 2


def test_task_workflow_requires_acceptance_and_qc_and_unlocks_departments_in_order(client_for, domain):
    order = domain['order']
    assert client_for('manager').post(f'/api/orders/{order.pk}/start/', {}, format='json').status_code == 200
    pick = order.tasks.get(stage='pick')
    ship = order.tasks.get(stage='ship')
    picker, dispatcher = client_for('picker'), client_for('dispatcher')
    pick_url, ship_url = f'/api/tasks/{pick.pk}/advance/', f'/api/tasks/{ship.pk}/advance/'
    assert dispatcher.post(ship_url, {'action': 'accept'}, format='json').status_code == 400
    assert picker.post(pick_url, {'action': 'complete', 'note': 'ОК'}, format='json').status_code == 400
    assert dispatcher.post(pick_url, {'action': 'accept'}, format='json').status_code == 404
    assert picker.post(pick_url, {'action': 'accept'}, format='json').status_code == 200
    assert picker.post(pick_url, {'action': 'reject', 'note': ''}, format='json').status_code == 400
    assert picker.post(pick_url, {'action': 'reject', 'note': 'Проверить маркировку'}, format='json').status_code == 200
    ship.refresh_from_db()
    assert ship.status == 'waiting'
    assert picker.post(pick_url, {'action': 'accept'}, format='json').status_code == 200
    assert picker.post(pick_url, {'action': 'complete', 'note': 'Комплектация проверена'}, format='json').status_code == 200
    order.refresh_from_db()
    assert order.status == 'ready'
    assert dispatcher.post(ship_url, {'action': 'accept'}, format='json').status_code == 200
    assert dispatcher.post(ship_url, {'action': 'complete', 'note': 'Передано курьеру'}, format='json').status_code == 200
    order.refresh_from_db()
    assert order.status == 'shipped'
    assert dispatcher.post(ship_url, {'action': 'complete', 'note': 'Повтор'}, format='json').status_code == 400


@pytest.mark.parametrize('invalid_note', [None, {}, [], 123])
def test_qc_note_must_be_a_real_nonempty_string(client_for, domain, invalid_note):
    order = domain['order']
    client_for('manager').post(f'/api/orders/{order.pk}/start/', {}, format='json')
    pick = order.tasks.get(stage='pick')
    picker = client_for('picker')
    url = f'/api/tasks/{pick.pk}/advance/'
    assert picker.post(url, {'action': 'accept'}, format='json').status_code == 200
    assert picker.post(url, {'action': 'complete', 'note': invalid_note}, format='json').status_code == 400


def test_payment_debt_refund_limits_and_expense_accounting(client_for, domain):
    client = client_for('accountant')
    order_id = domain['order'].pk
    payment = {'order': order_id, 'kind': 'payment', 'amount': '200.00', 'note': 'Предоплата'}
    assert client.post('/api/finance/', payment, format='json').status_code == 201
    assert client.post('/api/finance/', dict(payment, amount='101.00'), format='json').status_code == 400
    assert client.post('/api/finance/', {'order': order_id, 'kind': 'refund', 'amount': '201.00'}, format='json').status_code == 400
    assert client.post('/api/finance/', {'order': order_id, 'kind': 'refund', 'amount': '50.00'}, format='json').status_code == 201
    assert client.post('/api/finance/', {'kind': 'expense', 'amount': '25.00', 'note': 'Доставка'}, format='json').status_code == 201
    data = client.get(f'/api/orders/{order_id}/').json()
    assert Decimal(data['paid']) == Decimal('150.00')
    assert Decimal(data['debt']) == Decimal('150.00')
    assert FinanceEntry.objects.count() == 3


def test_analyst_cannot_write_business_objects(client_for, domain):
    client = client_for('analyst')
    assert client.get('/api/orders/').status_code == 200
    assert client.get('/api/finance/').status_code == 200
    assert client.get('/api/products/').status_code == 200
    assert client.post('/api/products/', {'sku': 'A', 'name': 'Forbidden', 'price': '1.00'}, format='json').status_code == 403
    assert client.post('/api/materials/', {'sku': 'M', 'name': 'Forbidden', 'kind': 'raw', 'unit': 'г'}, format='json').status_code == 403
    assert client.post('/api/finance/', {'order': domain['order'].pk, 'kind': 'payment', 'amount': '1.00'}, format='json').status_code == 403
    assert client.post(f"/api/orders/{domain['order'].pk}/start/", {}, format='json').status_code == 403


def test_audit_contains_field_changes_and_never_passwords(client_for, users):
    client = client_for('superadmin')
    employee_password = 'UniqueSecretGel2026!'
    response = client.post('/api/employees/', {'username': 'new-worker', 'first_name': 'Ольга',
                                             'password': employee_password, 'role': 'filler'}, format='json')
    assert response.status_code == 201
    assert 'password' not in response.json()
    employee = User.objects.get(username='new-worker')
    assert employee.check_password(employee_password)
    event = AuditEvent.objects.filter(entity='user', entity_id=str(employee.pk), action='create').get()
    assert event.actor_id == users['superadmin'].pk
    assert event.details['after']['first_name'] == 'Ольга'
    audit_text = json.dumps(list(AuditEvent.objects.values('details')), ensure_ascii=False)
    assert employee_password not in audit_text
    assert employee.password not in audit_text
    assert '"password"' not in audit_text


def test_notification_objects_cannot_be_read_by_another_employee(client_for, users):
    note = Notification.objects.create(user=users['manager2'], title='Секретное назначение')
    client = client_for('manager')
    assert client.post(f'/api/notifications/{note.pk}/read/', {}, format='json').status_code == 404
    assert client.get('/api/notifications/').json() == []
    note.refresh_from_db()
    assert not note.read


def test_incoming_messages_are_idempotent_and_preserve_manager_ownership(users, domain):
    incoming = IncomingMessage(provider='telegram', external_id='event-1', text='Нужен гель',
                               phone='8 999 111 22 33', customer_name='Другое имя')
    first = ingest_verified_message(incoming)
    second = ingest_verified_message(incoming)
    assert first.pk == second.pk
    assert Message.objects.filter(external_id='telegram:event-1').count() == 1
    assert first.customer_id == domain['customer'].pk
    domain['customer'].refresh_from_db()
    assert domain['customer'].manager_id == users['manager'].pk
    assert domain['customer'].name == 'Наша студия'
    assert Notification.objects.filter(user=users['manager'], title='Новое сообщение клиента').count() == 1


def test_incoming_message_with_conflicting_contacts_requires_manual_review(users, domain):
    domain['other'].email = 'other@example.com'
    domain['other'].save()
    incoming = IncomingMessage(provider='whatsapp', external_id='collision', text='Hello',
                               phone=domain['customer'].phone, email='other@example.com')
    with pytest.raises(ValueError, match='разным клиентам'):
        ingest_verified_message(incoming)
    assert not Message.objects.filter(external_id='whatsapp:collision').exists()


def test_backup_restore_requires_exact_confirmation_and_current_password(client_for):
    client = client_for('superadmin')
    assert client.post('/api/backups/restore/', {'backup_id': 999, 'password': PASSWORD}, format='json').status_code == 400
    assert client.post('/api/backups/restore/', {'backup_id': 999, 'password': 'wrong',
                                               'confirmation': 'ВОССТАНОВИТЬ'}, format='json').status_code == 400


@pytest.mark.parametrize('password', [None, {}, [], 123])
def test_restore_rejects_malformed_password_without_server_error(client_for, password):
    client = client_for('superadmin')
    client.raise_request_exception = False
    response = client.post('/api/backups/restore/', {
        'backup_id': 999, 'password': password, 'confirmation': 'ВОССТАНОВИТЬ',
    }, format='json')
    assert response.status_code == 400


@pytest.mark.parametrize('reason', [None, {}, [], 123, '   '])
def test_stock_adjustment_requires_nonempty_reason_string(client_for, domain, reason):
    response = client_for('admin').post(f"/api/materials/{domain['raw'].pk}/adjust/", {
        'quantity': '1.000', 'reason': reason,
    }, format='json')
    assert response.status_code == 400
    domain['raw'].refresh_from_db()
    assert domain['raw'].quantity == Decimal('100.000')


def test_stock_adjustment_rejects_overflow_and_preserves_ledger(client_for, domain):
    material = domain['raw']
    material.quantity = Decimal('99999999999.000')
    material.save()
    response = client_for('admin').post(f'/api/materials/{material.pk}/adjust/', {
        'quantity': '1.000', 'reason': 'Поступление',
    }, format='json')
    assert response.status_code == 400
    material.refresh_from_db()
    assert material.quantity == Decimal('99999999999.000')
    assert not StockMovement.objects.filter(material=material).exists()


@pytest.mark.parametrize('identifier', [{}, [], 'unknown'])
def test_assignment_rejects_malformed_manager_id_without_server_error(client_for, domain, identifier):
    client = client_for('admin')
    client.raise_request_exception = False
    response = client.post(f"/api/customers/{domain['customer'].pk}/assign/", {
        'manager_id': identifier,
    }, format='json')
    assert response.status_code in (400, 404)


@pytest.mark.parametrize('identifier', [{}, [], 'unknown'])
def test_restore_rejects_malformed_backup_id_without_server_error(client_for, identifier):
    client = client_for('superadmin')
    client.raise_request_exception = False
    response = client.post('/api/backups/restore/', {
        'backup_id': identifier, 'password': PASSWORD, 'confirmation': 'ВОССТАНОВИТЬ',
    }, format='json')
    assert response.status_code in (400, 404)
