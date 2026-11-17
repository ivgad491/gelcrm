"""Deterministic, repeatable demonstration data; never reset existing passwords."""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from crm.models import (
    AuditEvent, Customer, FinanceEntry, Material, Message, Notification,
    Order, Product, ProductionTask, Recipe, RecipeComponent, StockMovement, User,
)


class Command(BaseCommand):
    help = "Создать демонстрационных сотрудников и производственные данные (повторный запуск безопасен)."

    @transaction.atomic
    def handle(self, *args, **options):
        names = {
            "superadmin": "Александра Волкова",
            "admin": "Михаил Соколов",
            "manager": "Анна Смирнова",
            "colorist": "Дарья Морозова",
            "filler": "Иван Кузнецов",
            "labeler": "Елена Павлова",
            "picker": "Артём Орлов",
            "dispatcher": "Олег Фёдоров",
            "accountant": "Мария Белова",
            "analyst": "Софья Лебедева",
        }
        users = {}
        for role, name in names.items():
            user, created = User.objects.get_or_create(
                username=role,
                defaults={"first_name": name.split()[0], "last_name": name.split()[1],
                          "role": role, "is_staff": role == "superadmin", "is_superuser": role == "superadmin"},
            )
            if created:
                user.set_password("DemoGel2026!")
                user.save()
            users[role] = user

        if AuditEvent.objects.filter(action="demo.seed", entity="system", entity_id="demo-v1").exists():
            self.stdout.write(self.style.SUCCESS("Демонстрационные данные уже существуют. Пароли не изменены."))
            return

        admin = users["superadmin"]
        manager = users["manager"]
        material_specs = [
            ("RAW-BASE", "Прозрачная гелевая основа", "raw", "кг", "25.000", "5.000"),
            ("RAW-PINK", "Пигмент Rose Quartz", "raw", "кг", "2.500", "0.500"),
            ("RAW-PLUM", "Пигмент Plum", "raw", "кг", "0.280", "0.500"),
            ("RAW-BLUE", "Пигмент Sky", "raw", "кг", "1.800", "0.500"),
            ("GEL-CLEAR", "Готовый прозрачный гель", "gel", "кг", "18.000", "3.000"),
            ("JAR-15", "Баночка матовая 15 мл", "jar", "шт", "1200.000", "300.000"),
            ("PACK-15", "Коробка и этикетка 15 мл", "packaging", "шт", "850.000", "200.000"),
        ]
        materials = {}
        for sku, name, kind, unit, quantity, minimum in material_specs:
            material, created = Material.objects.get_or_create(
                sku=sku, defaults={"name": name, "kind": kind, "unit": unit,
                                   "quantity": Decimal(quantity), "min_quantity": Decimal(minimum)},
            )
            materials[sku] = material
            if created:
                StockMovement.objects.create(material=material, quantity=material.quantity,
                                             reason="Начальные остатки демонстрационного склада", actor=admin)

        recipe_specs = [
            ("Rose Quartz · 15 мл", "RAW-PINK", "Пыльно-розовый, полупрозрачный. Контроль: ровный цвет, отсутствие пузырьков."),
            ("Plum Velvet · 15 мл", "RAW-PLUM", "Глубокий сливовый, плотность 90%. Контроль выкраски после полимеризации."),
            ("Sky Blue · 15 мл", "RAW-BLUE", "Светло-голубой холодного подтона. Эталон хранится у колеровщика."),
        ]
        recipes = []
        for name, pigment, description in recipe_specs:
            recipe, _ = Recipe.objects.get_or_create(
                name=name, version=1, defaults={"description": description, "created_by": users["colorist"]},
            )
            for sku, quantity in [("RAW-BASE", "0.014"), (pigment, "0.001"), ("JAR-15", "1"), ("PACK-15", "1")]:
                RecipeComponent.objects.get_or_create(recipe=recipe, material=materials[sku],
                                                     defaults={"quantity": Decimal(quantity)})
            recipes.append(recipe)

        products = []
        for sku, name, price, stock, recipe in [
            ("GEL-ROSE-15", "Гель Rose Quartz · 15 мл", "650.00", "160", recipes[0]),
            ("GEL-MILK-15", "Гель Milky White · 15 мл", "590.00", "240", None),
            ("GEL-BASE-15", "База Clear Base · 15 мл", "520.00", "80", None),
            ("GEL-PLUM-15", "Гель Plum Velvet · 15 мл", "690.00", "35", recipes[1]),
        ]:
            product, created = Product.objects.get_or_create(
                sku=sku, defaults={"name": name, "price": Decimal(price), "stock": Decimal(stock), "recipe": recipe},
            )
            if created:
                StockMovement.objects.create(product=product, quantity=product.stock,
                                             reason="Начальные остатки демонстрационного каталога", actor=admin)
            products.append(product)

        customer_specs = [
            ("Студия ногтей LUNA", "+79991230001", "telegram", manager),
            ("Салон PINK ROOM", "+79991230002", "whatsapp", manager),
            ("Анна · Nail Studio", "+79991230003", "instagram", manager),
            ("Beauty Lab", "+79991230004", "manual", manager),
            ("Новый клиент · Марина", "+79991230005", "manual", None),
        ]
        customers = []
        for name, phone, channel, owner in customer_specs:
            customer, _ = Customer.objects.get_or_create(phone=phone,
                defaults={"name": name, "channel": channel, "manager": owner})
            customers.append(customer)

        orders = []
        order_specs = [
            (customers[0], "custom", None, recipes[0], 120, "650", "production", "Пыльная роза, полупрозрачный, без шиммера", "Образец согласован. Сохранить оттенок для повторного заказа."),
            (customers[1], "standard", products[1], None, 24, "590", "draft", "", "Добавить каталог новых оттенков."),
            (customers[2], "custom", None, recipes[1], 60, "690", "ready", "Сливовый, плотность 90%", "Контроль качества пройден, упаковать в транспортную коробку."),
            (customers[3], "standard", products[2], None, 40, "520", "shipped", "", "Отправлено курьером, доставка до двери."),
            (customers[0], "standard", products[0], None, 80, "650", "draft", "", "Повторный заказ популярного оттенка."),
            (customers[1], "custom", None, recipes[2], 100, "670", "draft", "Холодной небесно-голубой, без блёсток", "Ожидает подтверждения выкраски."),
        ]
        for customer, kind, product, recipe, quantity, price, status, color, comment in order_specs:
            order, created = Order.objects.get_or_create(
                customer=customer, comment=comment,
                defaults={"manager": manager, "kind": kind, "product": product, "recipe": recipe,
                          "quantity": quantity, "unit_price": Decimal(price), "status": status,
                          "color_parameters": color},
            )
            orders.append(order)
            if created and kind == "custom" and status in {"production", "ready", "shipped"}:
                for component in recipe.components.select_related("material"):
                    material = component.material
                    used = component.quantity * quantity
                    material.quantity -= used
                    material.save(update_fields=["quantity"])
                    StockMovement.objects.create(material=material, order=order, quantity=-used,
                                                 reason="Материалы для производственного заказа", actor=users["colorist"])
            elif created and kind == "standard" and status == "shipped":
                product.stock -= quantity
                product.save(update_fields=["stock", "updated_at"])
                StockMovement.objects.create(product=product, order=order, quantity=-quantity,
                                             reason="Комплектация стандартного заказа", actor=users["picker"])

        stage_roles = {"color": "colorist", "fill": "filler", "label": "labeler", "pick": "picker", "ship": "dispatcher"}
        for order in [orders[0], orders[2], orders[3]]:
            stages = ["color", "fill", "label", "pick", "ship"] if order.kind == "custom" else ["pick", "ship"]
            for stage in stages:
                if order.status == "shipped":
                    state = "completed"
                elif order.status == "ready":
                    state = "pending" if stage == "ship" else "completed"
                else:
                    state = "accepted" if stage == "color" else "waiting"
                ProductionTask.objects.get_or_create(order=order, stage=stage,
                    defaults={"status": state, "assigned_to": users[stage_roles[stage]],
                              "note": "Контроль качества: выкраска соответствует эталону." if state == "completed" else ""})

        for order, amount in [(orders[0], "39000"), (orders[2], "41400"), (orders[3], "20800")]:
            FinanceEntry.objects.get_or_create(order=order, kind="payment", amount=Decimal(amount),
                                              defaults={"actor": users["accountant"], "note": "Платёж по заказу"})
        FinanceEntry.objects.get_or_create(order=orders[3], kind="refund", amount=Decimal("1040"),
                                          defaults={"actor": users["accountant"], "note": "Частичный возврат за две единицы"})
        FinanceEntry.objects.get_or_create(kind="expense", amount=Decimal("12500"), note="Закупка транспортной упаковки",
                                          defaults={"actor": users["accountant"]})

        for customer, text in [
            (customers[0], "Анна, добрый день! Подтверждаем образец Rose Quartz. Можно запускать 120 баночек."),
            (customers[1], "Пришлите, пожалуйста, стоимость доставки заказа и сроки изготовления голубого оттенка."),
            (customers[4], "Здравствуйте! Хочу заказать гель с нашим оттенком. С чего начать?"),
        ]:
            Message.objects.get_or_create(customer=customer, text=text,
                                          defaults={"channel": customer.channel, "incoming": True})
        Message.objects.get_or_create(customer=customers[0], text="Добрый день! Передала заказ в колеровку. Сообщу о готовности.",
                                      defaults={"author": manager, "channel": "manual", "incoming": False})
        Notification.objects.get_or_create(user=manager, title="Новое сообщение от PINK ROOM",
                                           defaults={"text": "Клиент уточняет доставку и сроки колеровки."})
        Notification.objects.get_or_create(user=users["dispatcher"], title=f"Заказ #{orders[2].pk} готов к отправке",
                                           defaults={"text": "Производство и комплектация завершены. Примите задание."})
        Notification.objects.get_or_create(user=admin, title="Низкий остаток пигмента Plum",
                                           defaults={"text": "Остаток ниже минимального. Запланируйте закупку."})
        AuditEvent.objects.create(actor=admin, action="demo.seed", entity="system", entity_id="demo-v1",
                                  details={"description": "Созданы демонстрационные данные", "orders": len(orders)})
        self.stdout.write(self.style.SUCCESS("Готово: 10 сотрудников, склад, рецептуры, клиенты, 6 заказов и переписки."))
        self.stdout.write("Логин: superadmin; пароль: DemoGel2026! (для новых демонстрационных пользователей).")
