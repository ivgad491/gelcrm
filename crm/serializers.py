from decimal import Decimal
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Max
from rest_framework import serializers
from .models import (User, Customer, Message, Material, Recipe, RecipeComponent, Product, Order,
                     ProductionTask, StockMovement, FinanceEntry, Notification, AuditEvent, normalize_phone)
from .permissions import ADMIN_ROLES

class StrictTextField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError('Ожидается текстовая строка.')
        return super().to_internal_value(data)

class AssignmentInput(serializers.Serializer):
    manager_id = serializers.IntegerField(min_value=1, allow_null=True)

class TaskActionInput(serializers.Serializer):
    action = serializers.ChoiceField(choices=['accept', 'complete', 'reject'])
    note = StrictTextField(required=False, allow_blank=True, default='', max_length=5000)

class AdjustmentInput(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3)
    reason = StrictTextField(max_length=500)

class RestoreInput(serializers.Serializer):
    backup_id = serializers.IntegerField(min_value=1)
    confirmation = StrictTextField()
    password = StrictTextField(write_only=True, trim_whitespace=False)

class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'role', 'is_active', 'password']
    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        return value
    def validate(self, attrs):
        if not self.instance and not attrs.get('password'):
            raise serializers.ValidationError({'password': 'Укажите пароль.'})
        if attrs.get('password'):
            candidate = User(username=attrs.get('username', getattr(self.instance, 'username', '')),
                             first_name=attrs.get('first_name', getattr(self.instance, 'first_name', '')))
            try:
                validate_password(attrs['password'], candidate)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({'password': exc.messages})
        if self.instance and self.instance.pk == self.context['request'].user.pk:
            if attrs.get('is_active') is False or attrs.get('role', 'superadmin') != 'superadmin':
                raise serializers.ValidationError('Нельзя отключить или понизить собственную учётную запись.')
        return attrs
    def create(self, validated_data):
        return User.objects.create_user(**validated_data)
    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance

class CustomerSerializer(serializers.ModelSerializer):
    manager_name = serializers.CharField(source='manager.first_name', read_only=True, default='')
    class Meta:
        model = Customer
        fields = ['id', 'name', 'phone', 'email', 'channel', 'manager', 'manager_name', 'created_at']
        read_only_fields = ['manager', 'created_at']
        extra_kwargs = {'phone': {'validators': []}}
    def validate_phone(self, value):
        try:
            normalized = normalize_phone(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        if normalized and Customer.objects.filter(phone=normalized).exclude(pk=self.instance.pk if self.instance else None).exists():
            raise serializers.ValidationError('Клиент с таким телефоном уже существует.')
        return normalized
    def validate_email(self, value):
        value = value.strip().lower()
        if value and Customer.objects.filter(email__iexact=value).exclude(pk=self.instance.pk if self.instance else None).exists():
            raise serializers.ValidationError('Клиент с таким email уже существует.')
        return value
    def validate(self, attrs):
        if not attrs.get('phone', getattr(self.instance, 'phone', None)) and not attrs.get('email', getattr(self.instance, 'email', '')):
            raise serializers.ValidationError('Укажите телефон или email для защиты от дублей.')
        return attrs

class MessageSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source='author.first_name', read_only=True, default='Клиент')
    class Meta:
        model = Message
        fields = ['id', 'customer', 'author', 'author_name', 'text', 'channel', 'incoming', 'created_at']
        read_only_fields = ['id', 'customer', 'author', 'author_name', 'channel', 'incoming', 'created_at']

class MaterialSerializer(serializers.ModelSerializer):
    class Meta:
        model = Material
        fields = '__all__'
        read_only_fields = ['quantity']

class ComponentSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source='material.name', read_only=True)
    unit = serializers.CharField(source='material.unit', read_only=True)
    class Meta:
        model = RecipeComponent
        fields = ['material', 'material_name', 'unit', 'quantity']
    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError('Количество компонента должно быть положительным.')
        return value

class RecipeSerializer(serializers.ModelSerializer):
    components = ComponentSerializer(many=True)
    class Meta:
        model = Recipe
        fields = ['id', 'name', 'description', 'version', 'components', 'created_at']
        read_only_fields = ['version', 'created_at']
    def validate_components(self, value):
        if not value or len({v['material'].pk for v in value}) != len(value):
            raise serializers.ValidationError('Укажите материалы без повторений.')
        return value
    def create(self, validated_data):
        components = validated_data.pop('components')
        version = (Recipe.objects.filter(name=validated_data['name']).aggregate(last=Max('version'))['last'] or 0) + 1
        recipe = Recipe.objects.create(created_by=self.context['request'].user, version=version, **validated_data)
        for component in components:
            RecipeComponent.objects.create(recipe=recipe, **component)
        return recipe

class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ['id', 'sku', 'name', 'price', 'stock', 'recipe', 'updated_at']
        read_only_fields = ['stock', 'updated_at']

class OrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True, default='Индивидуальная колеровка')
    manager_name = serializers.CharField(source='manager.first_name', read_only=True, default='')
    recipe_name = serializers.CharField(source='recipe.name', read_only=True, default='')
    total = serializers.SerializerMethodField()
    paid = serializers.SerializerMethodField()
    debt = serializers.SerializerMethodField()
    class Meta:
        model = Order
        fields = ['id', 'customer', 'customer_name', 'manager', 'manager_name', 'kind', 'product', 'product_name',
                  'recipe', 'recipe_name', 'quantity', 'unit_price', 'status', 'color_parameters', 'comment', 'reference_photo',
                  'created_at', 'updated_at', 'total', 'paid', 'debt']
        read_only_fields = ['manager', 'status', 'created_at', 'updated_at']
    def get_total(self, obj):
        return str(obj.quantity * obj.unit_price)
    def get_paid(self, obj):
        return str(sum((v.amount if v.kind == 'payment' else -v.amount if v.kind == 'refund' else Decimal('0')
                       for v in obj.finance_entries.all()), Decimal('0')))
    def get_debt(self, obj):
        return str(obj.quantity * obj.unit_price - Decimal(self.get_paid(obj)))
    def validate(self, attrs):
        customer = attrs.get('customer')
        user = self.context['request'].user
        if customer and user.role == 'manager' and customer.manager_id != user.id:
            raise serializers.ValidationError('Заказ доступен только закреплённому менеджеру. Назначение выполняет администратор.')
        if attrs.get('kind') == 'standard' and not attrs.get('product'):
            raise serializers.ValidationError({'product': 'Выберите товар каталога.'})
        if attrs.get('kind') == 'custom' and not attrs.get('recipe'):
            raise serializers.ValidationError({'recipe': 'Выберите отдельную рецептуру.'})
        if attrs.get('kind') == 'custom' and attrs.get('product'):
            raise serializers.ValidationError({'product': 'Для колеровки укажите рецептуру вместо товара.'})
        if attrs.get('quantity', 1) <= 0 or attrs.get('unit_price', 0) < 0:
            raise serializers.ValidationError('Количество должно быть положительным, цена неотрицательной.')
        return attrs

class TaskSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='order.customer.name', read_only=True)
    quantity = serializers.IntegerField(source='order.quantity', read_only=True)
    assigned_name = serializers.CharField(source='assigned_to.first_name', read_only=True, default='')
    class Meta:
        model = ProductionTask
        fields = '__all__'

class FinanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinanceEntry
        fields = ['id', 'order', 'kind', 'amount', 'note', 'actor', 'created_at']
        read_only_fields = ['actor', 'created_at']
    def validate(self, attrs):
        if attrs['amount'] <= 0:
            raise serializers.ValidationError('Сумма должна быть положительной.')
        if attrs['kind'] != 'expense' and not attrs.get('order'):
            raise serializers.ValidationError('Для оплаты или возврата укажите заказ.')
        return attrs

class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'title', 'text', 'read', 'created_at']

class AuditSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source='actor.username', read_only=True, default='Система')
    class Meta:
        model = AuditEvent
        fields = '__all__'

class MovementSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockMovement
        fields = '__all__'
