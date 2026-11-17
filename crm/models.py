"""Persistent CRM domain models shared by REST API and background workers."""

import re
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


def normalize_phone(value):
    """Store a canonical phone, treating ten-digit domestic numbers as Russian."""
    if not value or not str(value).strip():
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not 7 <= len(digits) <= 15:
        raise ValidationError({"phone": "Введите корректный телефон (7–15 цифр)."})
    return "+" + digits


class User(AbstractUser):
    class Role(models.TextChoices):
        SUPERADMIN = "superadmin", "Главный администратор"
        ADMIN = "admin", "Администратор"
        MANAGER = "manager", "Менеджер"
        COLORIST = "colorist", "Колеровщик"
        FILLER = "filler", "Работник розлива"
        LABELER = "labeler", "Работник маркировки"
        PICKER = "picker", "Сборщик заказов"
        DISPATCHER = "dispatcher", "Отправщик доставки"
        ACCOUNTANT = "accountant", "Бухгалтер"
        ANALYST = "analyst", "Аналитик"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MANAGER, db_index=True)
    email = models.EmailField(blank=True)

    def __str__(self):
        return self.get_full_name() or self.username


class Timestamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Customer(Timestamped):
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    email = models.EmailField(blank=True)
    channel = models.CharField(max_length=30, default="manual")
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="customers")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["manager", "created_at"])]
        constraints = [models.UniqueConstraint(Lower("email"), condition=~Q(email=""),
                                               name="customer_email_casefold_unique")]

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone)
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Message(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                               null=True, blank=True, related_name="messages")
    text = models.TextField()
    channel = models.CharField(max_length=30, default="manual")
    external_id = models.CharField(max_length=250, blank=True, null=True, unique=True)
    incoming = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["customer", "created_at"])]


class Material(models.Model):
    class Kind(models.TextChoices):
        RAW = "raw", "Сырьё"
        GEL = "gel", "Готовый гель"
        JAR = "jar", "Баночки"
        PACKAGING = "packaging", "Упаковка"

    sku = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    unit = models.CharField(max_length=20, default="шт")
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    min_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))

    class Meta:
        ordering = ["kind", "name"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gte=0), name="material_quantity_nonnegative"),
            models.CheckConstraint(condition=Q(min_quantity__gte=0), name="material_minimum_nonnegative"),
        ]

    def __str__(self):
        return self.name


class Recipe(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="recipes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["name", "version"], name="recipe_name_version_unique"),
            models.CheckConstraint(condition=Q(version__gt=0), name="recipe_version_positive"),
        ]

    def __str__(self):
        return f"{self.name} · v{self.version}"


class RecipeComponent(models.Model):
    recipe = models.ForeignKey(Recipe, on_delete=models.PROTECT, related_name="components")
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="recipe_components")
    quantity = models.DecimalField(max_digits=14, decimal_places=3)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["recipe", "material"], name="recipe_material_unique"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="recipe_quantity_positive"),
        ]


class Product(models.Model):
    sku = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    stock = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    recipe = models.ForeignKey(Recipe, on_delete=models.PROTECT, null=True, blank=True, related_name="products")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=Q(price__gte=0), name="product_price_nonnegative"),
            models.CheckConstraint(condition=Q(stock__gte=0), name="product_stock_nonnegative"),
        ]

    def __str__(self):
        return self.name


class Order(Timestamped):
    class Kind(models.TextChoices):
        STANDARD = "standard", "Товар каталога"
        CUSTOM = "custom", "Индивидуальная колеровка"

    class Status(models.TextChoices):
        DRAFT = "draft", "Новый"
        PRODUCTION = "production", "В производстве"
        READY = "ready", "Готов к отправке"
        SHIPPED = "shipped", "Отправлен"
        CANCELLED = "cancelled", "Отменён"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders")
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="orders")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.STANDARD)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, null=True, blank=True, related_name="orders")
    recipe = models.ForeignKey(Recipe, on_delete=models.PROTECT, null=True, blank=True, related_name="orders")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    color_parameters = models.TextField(blank=True)
    comment = models.TextField(blank=True)
    reference_photo = models.URLField(max_length=1000, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["manager", "status"])]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="order_quantity_positive"),
            models.CheckConstraint(condition=Q(unit_price__gte=0), name="order_price_nonnegative"),
            models.CheckConstraint(
                condition=Q(kind="standard", product__isnull=False) | Q(kind="custom"),
                name="standard_order_has_product",
            ),
        ]

    @property
    def total(self):
        return self.quantity * self.unit_price

    def __str__(self):
        return f"Заказ #{self.pk}"


class ProductionTask(Timestamped):
    class Stage(models.TextChoices):
        COLOR = "color", "Колеровка"
        FILL = "fill", "Розлив"
        LABEL = "label", "Маркировка"
        PICK = "pick", "Комплектация"
        SHIP = "ship", "Отправка"

    class Status(models.TextChoices):
        WAITING = "waiting", "Ожидает предыдущий этап"
        PENDING = "pending", "Готов к приёмке"
        ACCEPTED = "accepted", "В работе"
        COMPLETED = "completed", "Завершён"
        REJECTED = "rejected", "Возвращён на доработку"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="tasks")
    stage = models.CharField(max_length=20, choices=Stage.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.WAITING, db_index=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="production_tasks")
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [models.UniqueConstraint(fields=["order", "stage"], name="order_stage_unique")]
        indexes = [models.Index(fields=["stage", "status"])]


class StockMovement(models.Model):
    material = models.ForeignKey(Material, on_delete=models.PROTECT, null=True, blank=True, related_name="movements")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, null=True, blank=True, related_name="movements")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True, related_name="stock_movements")
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    reason = models.CharField(max_length=500)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                              null=True, blank=True, related_name="stock_movements")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(material__isnull=False, product__isnull=True) |
                           Q(material__isnull=True, product__isnull=False)),
                name="stock_movement_exactly_one_resource",
            ),
            models.CheckConstraint(condition=~Q(quantity=0), name="stock_movement_nonzero"),
        ]


class FinanceEntry(models.Model):
    class Kind(models.TextChoices):
        PAYMENT = "payment", "Оплата"
        REFUND = "refund", "Возврат"
        EXPENSE = "expense", "Расход"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True, related_name="finance_entries")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    note = models.TextField(blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                              null=True, blank=True, related_name="finance_entries")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="finance_amount_positive"),
            models.CheckConstraint(condition=Q(kind="expense") | Q(order__isnull=False),
                                   name="finance_payment_refund_require_order"),
        ]


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=200)
    text = models.TextField(blank=True)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "read", "created_at"])]


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                              null=True, blank=True, related_name="audit_events")
    action = models.CharField(max_length=100)
    entity = models.CharField(max_length=100)
    entity_id = models.CharField(max_length=100, blank=True)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["entity", "entity_id", "created_at"])]


class BackupRecord(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "В очереди"
        RUNNING = "running", "Создаётся"
        SUCCESS = "success", "Готова"
        FAILED = "failed", "Ошибка"

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    filename = models.CharField(max_length=250, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="backup_requests")

    class Meta:
        ordering = ["-created_at"]


class ImportBatch(models.Model):
    class Status(models.TextChoices):
        PREVIEW = "preview", "Предпросмотр"
        APPLIED = "applied", "Применён"

    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="import_batches")
    filename = models.CharField(max_length=250)
    rows = models.JSONField(default=list)
    columns = models.JSONField(default=list)
    mapping = models.JSONField(default=dict)
    errors = models.JSONField(default=list)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PREVIEW)
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class CatalogHistory(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="catalog_history")
    batch = models.ForeignKey(ImportBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="history")
    old_price = models.DecimalField(max_digits=14, decimal_places=2)
    new_price = models.DecimalField(max_digits=14, decimal_places=2)
    old_stock = models.DecimalField(max_digits=14, decimal_places=3)
    new_stock = models.DecimalField(max_digits=14, decimal_places=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
