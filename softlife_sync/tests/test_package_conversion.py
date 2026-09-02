from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestPackageConversion(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env['product.product'].create({
            'name': 'Bagged powder',
            'uom_id': cls.env.ref('uom.product_uom_unit').id,
            'uom_po_id': cls.env.ref('uom.product_uom_unit').id,
            'is_storable': True,
            'package_content_quantity': 1120,
            'package_content_uom': 'g',
        })
        cls.recipe_model = cls.env['softlife.recipe.sync']

    def _recipe(self, **component_overrides):
        component = {
            'odoo_product_id': self.product.id,
            'quantity_per_unit': 100,
            'uom': 'g',
            'stock_quantity_per_unit': 100 / 1120,
            'stock_uom': 'unit',
            'package_content_quantity': 1120,
            'package_content_uom': 'g',
        }
        component.update(component_overrides)
        return {
            'recipe_id': 'recipe-1',
            'recipe_version_id': 'version-1',
            'component_hash': 'hash-1',
            'payload_contract_version': 2,
            'components': [component],
        }

    def test_converts_grams_to_fractional_inventory_units(self):
        component = self.recipe_model._component_values(self._recipe())[0]

        self.assertAlmostEqual(component['quantity'], 100 / 1120, places=9)
        self.assertEqual(component['uom'], self.product.uom_id)
        self.assertEqual(component['calculation'], '100 g / 1120 g = 0.0892857143 unit')

    def test_bom_freezes_exact_fraction_and_conversion_audit(self):
        sync = self.recipe_model.ensure_recipe(self._recipe())
        line = sync.bom_id.bom_line_ids

        self.assertEqual(len(line), 1)
        self.assertAlmostEqual(line.product_qty, 100 / 1120, places=12)
        self.assertAlmostEqual(line.softlife_stock_quantity_per_unit, 100 / 1120, places=9)
        self.assertEqual(line.softlife_dosage_quantity, 100)
        self.assertEqual(line.softlife_dosage_uom, 'g')
        self.assertEqual(line.softlife_package_content_quantity, 1120)
        self.assertEqual(line.softlife_package_content_uom, 'g')
        self.assertEqual(line.softlife_conversion_audit, '100 g / 1120 g = 0.0892857143 unit')

    def test_accepts_catalog_stock_quantity_name(self):
        recipe = self._recipe()
        recipe['components'][0]['stock_quantity'] = recipe['components'][0].pop(
            'stock_quantity_per_unit'
        )

        component = self.recipe_model._component_values(recipe)[0]

        self.assertAlmostEqual(component['quantity'], 100 / 1120, places=9)

    def test_derives_oca_secondary_uom_factor(self):
        secondary = self.product.product_tmpl_id.secondary_uom_ids.filtered(
            lambda unit: unit.code == 'SOFTLIFE_CONTENT'
        )

        self.assertEqual(len(secondary), 1)
        self.assertEqual(secondary.uom_id, self.env.ref('uom.product_uom_gram'))
        self.assertAlmostEqual(secondary.factor, 1 / 1120, places=12)

    def test_unit_uom_keeps_fractional_stock_precision(self):
        self.assertEqual(self.product.uom_id.rounding, 0.000001)
        self.assertEqual(self.product.uom_id, self.env.ref('uom.product_uom_unit'))
        precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')
        self.assertEqual(precision, 6)

    def test_package_configuration_preserves_existing_quant_and_inventory_uom(self):
        product = self.env['product.product'].create({
            'name': 'Existing ingredient',
            'uom_id': self.env.ref('uom.product_uom_unit').id,
            'uom_po_id': self.env.ref('uom.product_uom_unit').id,
            'is_storable': True,
        })
        quant = self.env['stock.quant'].create({
            'product_id': product.id,
            'location_id': self.env.ref('stock.stock_location_stock').id,
            'quantity': 7.25,
        })
        move = self.env['stock.move'].create({
            'name': 'Existing ingredient move',
            'product_id': product.id,
            'product_uom_qty': 3.5,
            'product_uom': product.uom_id.id,
            'location_id': self.env.ref('stock.stock_location_stock').id,
            'location_dest_id': self.env.ref('stock.stock_location_customers').id,
        })

        product.write({
            'package_content_quantity': 1120,
            'package_content_uom': 'g',
        })

        self.assertEqual(product.uom_id, self.env.ref('uom.product_uom_unit'))
        self.assertEqual(quant.quantity, 7.25)
        self.assertEqual(move.product_uom, self.env.ref('uom.product_uom_unit'))
        self.assertEqual(move.product_uom_qty, 3.5)

    def test_converts_package_content_across_weight_units(self):
        component = self.recipe_model._component_values(self._recipe(
            package_content_quantity=1.12,
            package_content_uom='kg',
        ))[0]

        self.assertAlmostEqual(component['quantity'], 100 / 1120, places=9)

    def test_rejects_weight_to_volume_package_typo(self):
        with self.assertRaisesRegex(ValidationError, 'incompatible with dosage UoM'):
            self.recipe_model._component_values(self._recipe(package_content_uom='ml'))

    def test_uses_frozen_snapshot_not_current_product_configuration(self):
        self.product.package_content_quantity = 5000

        component = self.recipe_model._component_values(self._recipe())[0]

        self.assertAlmostEqual(component['quantity'], 100 / 1120, places=9)

    def test_rejects_inconsistent_frozen_stock_quantity(self):
        with self.assertRaisesRegex(ValidationError, 'Frozen stock conversion is inconsistent'):
            self.recipe_model._component_values(self._recipe(stock_quantity_per_unit=100))

    def test_rejects_missing_package_snapshot(self):
        with self.assertRaisesRegex(ValidationError, 'Frozen package content is required'):
            self.recipe_model._component_values(self._recipe(
                package_content_quantity=None,
                package_content_uom=None,
            ))

    def test_rejects_legacy_contract_for_manufacturing(self):
        recipe = self._recipe()
        recipe['payload_contract_version'] = 1
        with self.assertRaisesRegex(ValidationError, 'payload_contract_version 2'):
            self.recipe_model._component_values(recipe)

    def test_catalog_sync_ignores_historical_contract(self):
        catalog = [{
            'payload_contract_version': 1,
            'recipe_versions': [dict(self._recipe(), payload_contract_version=1)],
        }]
        client = self.env['softlife.sync.client']
        with patch.object(type(client), '_api_catalog_pages', return_value=iter(catalog)):
            self.env['softlife.manufacturing.run'].action_sync_catalog()

        self.assertFalse(self.recipe_model.search([('recipe_version_id', '=', 'version-1')]))

    def test_package_content_fields_are_paired(self):
        with self.assertRaisesRegex(ValidationError, 'must either both be set or both be empty'):
            self.product.package_content_uom = False
