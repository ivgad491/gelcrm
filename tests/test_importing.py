import io
import zipfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from openpyxl import Workbook

from crm.importing import apply_import, preview_import
from crm.models import CatalogHistory, Product, Recipe, StockMovement, User


class CatalogueImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="import_admin", password="DemoGel2026!", role="superadmin")

    def preview(self, text, filename="catalog.csv", mapping=None):
        content = text.encode("utf-8") if isinstance(text, str) else text
        return preview_import(SimpleUploadedFile(filename, content), self.user, mapping)

    def test_preview_does_not_write_and_apply_keeps_recipe_and_history(self):
        recipe = Recipe.objects.create(name="Original formula", description="Protected")
        product = Product.objects.create(sku="G-1", name="Old name", price="100.00", stock="5.000", recipe=recipe)
        batch = self.preview("Артикул;Наименование;Цена;Остаток\nG-1;Розовый гель;125,50;8\nG-2;Молочный;90;2\n")
        self.assertEqual(batch.errors, [])
        product.refresh_from_db()
        self.assertEqual(product.price, Decimal("100.00"))
        self.assertEqual(Product.objects.count(), 1)
        result = apply_import(batch, self.user)
        self.assertEqual(result, {"created": 1, "updated": 1, "total": 2})
        product.refresh_from_db()
        self.assertEqual(product.recipe_id, recipe.pk)
        self.assertEqual(product.price, Decimal("125.50"))
        self.assertEqual(product.stock, Decimal("8"))
        history = CatalogHistory.objects.get(product=product)
        self.assertEqual(history.old_price, Decimal("100"))
        self.assertEqual(history.new_price, Decimal("125.50"))
        movements = StockMovement.objects.order_by("product__sku")
        self.assertEqual(list(movements.values_list("product__sku", "quantity")),
                         [("G-1", Decimal("3")), ("G-2", Decimal("2"))])
        self.assertEqual(movements[0].actor_id, self.user.pk)
        self.assertIn(str(batch.pk), movements[0].reason)
        with self.assertRaisesMessage(ValueError, "уже применён"):
            apply_import(batch, self.user)

    def test_invalid_or_duplicate_row_prevents_entire_import(self):
        batch = self.preview("sku,name,price,stock\nA,Valid,10,2\nA,Duplicate,20,3\nB,Invalid,-1,3\n")
        self.assertEqual(len(batch.errors), 2)
        with self.assertRaises(ValueError):
            apply_import(batch, self.user)
        self.assertFalse(Product.objects.exists())
        self.assertFalse(CatalogHistory.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_custom_mapping_and_omitted_values_keep_existing(self):
        product = Product.objects.create(sku="X", name="Keep name", price="15.00", stock="7.000")
        batch = self.preview("Identifier,Cost\nX,19.25\n", mapping={"sku": "Identifier", "price": "Cost"})
        self.assertFalse(batch.errors)
        apply_import(batch, self.user)
        product.refresh_from_db()
        self.assertEqual(product.name, "Keep name")
        self.assertEqual(product.price, Decimal("19.25"))
        self.assertEqual(product.stock, Decimal("7"))
        self.assertFalse(StockMovement.objects.exists())

    def test_stock_decrease_records_only_nonzero_delta(self):
        product = Product.objects.create(sku="LOWER", name="Existing", price="10", stock="8")
        batch = self.preview("sku,name,stock\nLOWER,Existing,3\nZERO,New empty,0\n")
        apply_import(batch, self.user)
        movement = StockMovement.objects.get()
        self.assertEqual(movement.product_id, product.pk)
        self.assertEqual(movement.quantity, Decimal("-5"))

    def test_mapping_can_be_corrected_at_apply(self):
        batch = self.preview("code,title\nZ,Custom\n")
        self.assertTrue(batch.errors)
        apply_import(batch, self.user, {"sku": "code", "name": "title"})
        self.assertEqual(Product.objects.get(sku="Z").name, "Custom")

    def test_commerceml_catalogue_and_offers_merge_by_id(self):
        batch = self.preview('''<?xml version="1.0" encoding="UTF-8"?>
            <КоммерческаяИнформация xmlns="urn:1C.ru:commerceml_2">
              <Каталог><Товары><Товар><Ид>external-1</Ид><Артикул>GEL-01</Артикул>
              <Наименование>Гель</Наименование></Товар></Товары></Каталог>
              <ПакетПредложений><Предложения><Предложение><Ид>external-1</Ид>
              <Цены><Цена><ЦенаЗаЕдиницу>125.25</ЦенаЗаЕдиницу></Цена></Цены>
              <Количество>3.5</Количество></Предложение></Предложения></ПакетПредложений>
            </КоммерческаяИнформация>''', "catalog.xml")
        self.assertEqual(len(batch.rows), 1)
        self.assertEqual(batch.errors, [])
        apply_import(batch, self.user)
        product = Product.objects.get(sku="GEL-01")
        self.assertEqual(product.price, Decimal("125.25"))
        self.assertEqual(product.stock, Decimal("3.5"))

    def test_xlsx_and_cp1251_csv(self):
        workbook = Workbook()
        workbook.active.append(["sku", "name", "price", "stock"])
        workbook.active.append(["EXCEL", "Гель Excel", 15, 4])
        output = io.BytesIO()
        workbook.save(output)
        batch = self.preview(output.getvalue(), "catalog.xlsx")
        self.assertEqual(batch.errors, [])
        apply_import(batch, self.user)
        batch = self.preview("Артикул;Наименование\nCP1251;Розовый\n".encode("cp1251"))
        apply_import(batch, self.user)
        self.assertEqual(Product.objects.count(), 2)

    def test_duplicate_offers_in_commerceml_are_errors(self):
        batch = self.preview('''<КоммерческаяИнформация>
            <Товар><Ид>A</Ид><Наименование>Гель</Наименование></Товар>
            <Предложение><Ид>A</Ид><ЦенаЗаЕдиницу>10</ЦенаЗаЕдиницу></Предложение>
            <Предложение><Ид>A</Ид><ЦенаЗаЕдиницу>20</ЦенаЗаЕдиницу></Предложение>
            </КоммерческаяИнформация>''', "catalog.xml")
        self.assertTrue(batch.errors)
        with self.assertRaises(ValueError):
            apply_import(batch, self.user)
        self.assertFalse(Product.objects.exists())

    def test_external_entities_and_expanded_xlsx_are_rejected(self):
        with self.assertRaisesMessage(ValueError, "небезопасный XML"):
            self.preview('''<!DOCTYPE x [<!ENTITY example SYSTEM "file:///etc/passwd">]>
                <x><Товар><Ид>X</Ид><Наименование>&example;</Наименование></Товар></x>''', "catalog.xml")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/worksheets/sheet1.xml", b"x" * (51 * 1024 * 1024))
        with self.assertRaisesMessage(ValueError, "Распакованный Excel"):
            self.preview(output.getvalue(), "bomb.xlsx")

    def test_precision_nan_and_negative_stock_are_errors(self):
        batch = self.preview("sku,name,price,stock\nA,Nan,NaN,1\nB,Fraction,0.001,2\nC,Negative,1,-2\n")
        self.assertEqual(len(batch.errors), 3)
        with self.assertRaises(ValueError):
            apply_import(batch, self.user)
        self.assertEqual(Product.objects.count(), 0)
