from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestManufacturingRun(TransactionCase):

    def test_remote_values_normalize_iso_datetimes_to_utc(self):
        values = self.env['softlife.manufacturing.run']._remote_values({
            'export_id': 'export-1',
            'period_from': '2026-07-08T22:00:00+00:00',
            'period_to': '2026-07-10T00:00:00+02:00',
            'manufacturing_contract_version': 2,
        })

        self.assertEqual(values['period_from'], '2026-07-08 22:00:00')
        self.assertEqual(values['period_to'], '2026-07-09 22:00:00')
        self.assertEqual(values['payload']['manufacturing_contract_version'], 2)

    def test_remote_values_reject_invalid_datetime(self):
        with self.assertRaisesRegex(ValidationError, 'invalid datetime'):
            self.env['softlife.manufacturing.run']._remote_values({
                'export_id': 'export-1',
                'period_from': 'not-a-datetime',
            })

    def test_processing_requires_warehouse_company_to_be_active(self):
        other_company = self.env['res.company'].create({'name': 'Other Manufacturing Company'})
        warehouse = self.env['stock.warehouse'].search([
            ('company_id', '=', other_company.id),
        ], limit=1)
        if not warehouse:
            warehouse = self.env['stock.warehouse'].create({
                'name': 'Other Manufacturing Warehouse',
                'code': 'OMW',
                'company_id': other_company.id,
            })
        run = self.env['softlife.manufacturing.run'].new({
            'payload': {'warehouses': [{'odoo_warehouse_id': warehouse.id}]},
        })

        with self.assertRaisesRegex(UserError, 'Switch the active Odoo company'):
            run._check_active_company()

    def test_processing_rejects_legacy_manufacturing_contract(self):
        run = self.env['softlife.manufacturing.run'].new({
            'payload': {'manufacturing_contract_version': 1, 'warehouses': [{}]},
        })

        with self.assertRaisesRegex(ValidationError, 'contract version 2'):
            run._process_documents()

    def test_repeated_rejection_does_not_replace_platform_result(self):
        run = self.env['softlife.manufacturing.run'].create({
            'export_id': 'export-retry',
            'platform_status': 'failed',
            'processing_state': 'result_pending',
            'platform_result': {'accepted': False, 'error': 'First failure'},
            'result_payload': {'accepted': False, 'error': 'Retry failure'},
        })
        client = self.env['softlife.sync.client']

        with patch.object(type(client), '_api_request') as request:
            run.action_retry_callback()

        request.assert_not_called()
        self.assertEqual(run.processing_state, 'failed')
        self.assertFalse(run.callback_error)

    def test_manufacturing_customer_uses_configured_vending_partner(self):
        partner = self.env['res.partner'].create({'name': 'Consumidor Final'})
        self.env['ir.config_parameter'].sudo().set_param('softlife.sync.default_partner_id', partner.id)

        customer = self.env['softlife.manufacturing.run']._manufacturing_customer()

        self.assertEqual(customer, partner)

    def test_manufacturing_customer_requires_configuration(self):
        self.env['ir.config_parameter'].sudo().set_param('softlife.sync.default_partner_id', '')

        with self.assertRaisesRegex(ValidationError, 'Vending Customer / Consumidor Final'):
            self.env['softlife.manufacturing.run']._manufacturing_customer()

    def test_source_orders_preserve_order_to_machine_mapping(self):
        run = self.env['softlife.manufacturing.run']
        sources = run._source_orders({'source_orders': [{
            'platform_order_id': 'order-id', 'order_code': 'S00124',
            'machine_id': 'machine-id', 'machine_imei': '860000000000001',
            'machine_name': 'Flowers',
        }]})

        self.assertEqual(sources[0]['order_code'], 'S00124')
        self.assertEqual(sources[0]['machine_id'], 'machine-id')
        sale = self.env['sale.order'].new({'softlife_source_orders': sources})
        sale._compute_softlife_source_summary()
        self.assertIn('S00124 - Flowers', sale.softlife_source_summary)
