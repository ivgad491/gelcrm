"""Write a field-level audit, with passwords and session data never persisted."""
from contextvars import ContextVar
from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver
from .models import AuditEvent

audit_actor = ContextVar('audit_actor', default=None)
EXCLUDE = {'password', 'last_login'}
AUDITED = {'user', 'customer', 'message', 'material', 'recipe', 'recipecomponent', 'product', 'order',
           'productiontask', 'stockmovement', 'financeentry', 'backuprecord', 'importbatch', 'cataloghistory'}

def snapshot(instance):
    result = {}
    for field in instance._meta.concrete_fields:
        if field.name in EXCLUDE or field.name in ('rows',):
            continue
        value = getattr(instance, field.attname)
        result[field.name] = value if value is None or isinstance(value, (str, int, bool, list, dict)) else str(value)
    return result

def eligible(sender):
    return sender._meta.app_label == 'crm' and sender._meta.model_name in AUDITED

@receiver(pre_save)
def remember_previous(sender, instance, raw=False, **kwargs):
    if not raw and eligible(sender):
        old = sender.objects.filter(pk=instance.pk).first() if instance.pk else None
        instance._audit_before = snapshot(old) if old else None

@receiver(post_save)
def audit_save(sender, instance, created, raw=False, **kwargs):
    if not raw and eligible(sender):
        actor = audit_actor.get()
        AuditEvent.objects.create(actor=actor, action='create' if created else 'update',
                                  entity=sender._meta.model_name, entity_id=str(instance.pk),
                                  details={'before': getattr(instance, '_audit_before', None), 'after': snapshot(instance)})

@receiver(post_delete)
def audit_delete(sender, instance, **kwargs):
    if eligible(sender):
        actor = audit_actor.get()
        if actor and sender._meta.model_name == 'user' and actor.pk == instance.pk:
            actor = None
        AuditEvent.objects.create(actor=actor, action='delete', entity=sender._meta.model_name,
                                  entity_id=str(instance.pk), details={'before': snapshot(instance)})
