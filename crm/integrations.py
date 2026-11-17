"""Provider boundaries. No unofficial scraping or simulated deliveries."""
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class IncomingMessage:
    provider: str
    external_id: str
    text: str
    phone: str = ''
    email: str = ''
    customer_name: str = ''

class MessengerAdapter(Protocol):
    def verify_webhook(self, body: bytes, headers: dict) -> bool: ...
    def parse(self, body: bytes) -> IncomingMessage: ...
    def send(self, recipient: str, text: str) -> str: ...

def integration_status():
    requirements = {
        'telegram': ('Telegram', 'Bot API: токен бота, HTTPS webhook; пользователь должен первым написать боту.'),
        'whatsapp': ('WhatsApp', 'Официальный Business Platform API, подтверждённый бизнес, шаблоны и окно переписки.'),
        'max': ('MAX', 'Официальный Bot API и разрешённый доступ платформы; возможности зависят от аккаунта.'),
        'instagram': ('Instagram', 'Официальный Meta API, профессиональный аккаунт, проверка приложения и разрешения.'),
    }
    return [{'id': key, 'name': label, 'status': 'not_configured', 'connected': False,
             'description': description, 'requirements': description}
            for key, (label, description) in requirements.items()]

def ingest_verified_message(message: IncomingMessage):
    """Called only by a future adapter AFTER signature/replay verification.

    A canonical contact is required. Unknown provider handles must not merge clients
    by a display name. This helper is deliberately not a public webhook endpoint.
    """
    from django.db import transaction
    from django.db.models import Q
    from .models import Customer, Message, Notification, normalize_phone
    from .services import notify_roles
    from .permissions import ADMIN_ROLES
    if not message.external_id or not message.text:
        raise ValueError('Нет идентификатора или текста сообщения.')
    phone = normalize_phone(message.phone)
    email = message.email.strip().lower()
    if not phone and not email:
        raise ValueError('Необходим подтверждённый телефон или email; сопоставьте контакт вручную.')
    external_id = f'{message.provider}:{message.external_id}'
    with transaction.atomic():
        existing = Message.objects.filter(external_id=external_id).first()
        if existing:
            return existing
        query = Q()
        if phone:
            query |= Q(phone=phone)
        if email:
            query |= Q(email__iexact=email)
        matches = list(Customer.objects.select_for_update().filter(query))
        if len(matches) > 1:
            raise ValueError('Телефон и email относятся к разным клиентам; требуется ручная проверка.')
        customer = matches[0] if matches else Customer.objects.create(
            name=message.customer_name or phone or email, phone=phone, email=email, channel=message.provider)
        result = Message.objects.create(customer=customer, text=message.text, incoming=True,
                                        channel=message.provider, external_id=external_id)
        if customer.manager_id:
            Notification.objects.create(user=customer.manager, title='Новое сообщение клиента', text=customer.name)
        else:
            notify_roles(ADMIN_ROLES | {'manager'}, 'Новое обращение в общей очереди', customer.name)
        return result
