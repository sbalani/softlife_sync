from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestManufacturingRun(TransactionCase):

    def test_remote_values_normalize_iso_datetimes_to_utc(self):
        values = self.env['softlife.manufacturing.run']._remote_values({
            'export_id': 'export-1',
            'period_from': '2026-07-08T22:00:00+00:00',
            'period_to': '2026-07-10T00:00:00+02:00',
        })

        self.assertEqual(values['period_from'], '2026-07-08 22:00:00')
        self.assertEqual(values['period_to'], '2026-07-09 22:00:00')

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
