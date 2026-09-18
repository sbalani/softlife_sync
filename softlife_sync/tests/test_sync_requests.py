from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestSyncRequests(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env['softlife.sync.client']

    def test_processes_platform_request_and_posts_success(self):
        Client = type(self.client)
        with patch.object(Client, '_api_request', side_effect=[
            {'request': {'id': 'd58e68dd-21a6-4a55-8cf4-d7a26bba1264', 'claim_token': '7cbd854e-3f88-4ca3-b86b-a4853adffbd3'}},
            {'request': {'status': 'completed'}},
        ]) as api, patch.object(Client, 'sync_all', return_value='Synced all warehouse stock.'), \
                patch.object(Client, '_acquire_sync_lock', return_value=True), \
                patch.object(type(self.client.env.cr), 'commit'):
            result = self.client.process_platform_sync_request()

        self.assertTrue(result['accepted'])
        self.assertEqual(api.call_args_list[1].args[:2], (
            'POST', '/api/internal/odoo/sync-requests/d58e68dd-21a6-4a55-8cf4-d7a26bba1264/result',
        ))
        self.assertEqual(api.call_args_list[1].kwargs['payload'], result)
        self.assertEqual(result['claim_token'], '7cbd854e-3f88-4ca3-b86b-a4853adffbd3')

    def test_reports_partial_sync_errors_as_failure(self):
        Client = type(self.client)
        summary = 'Synced 3 product(s). Errors: odoo_lot_stock: invalid product stock row'
        with patch.object(Client, '_api_request', side_effect=[
            {'request': {'id': '7cbd854e-3f88-4ca3-b86b-a4853adffbd3', 'claim_token': 'd58e68dd-21a6-4a55-8cf4-d7a26bba1264'}},
            {'request': {'status': 'failed'}},
        ]), patch.object(Client, 'sync_all', return_value=summary), \
                patch.object(Client, '_acquire_sync_lock', return_value=True), \
                patch.object(type(self.client.env.cr), 'commit'):
            result = self.client.process_platform_sync_request()

        self.assertFalse(result['accepted'])
        self.assertEqual(result['error'], summary)

    def test_reports_nonfatal_sync_warnings_as_success(self):
        Client = type(self.client)
        summary = 'Synced 3 product(s). Warnings: machines: ingredient has no linked Odoo product'
        with patch.object(Client, '_api_request', side_effect=[
            {'request': {'id': '7cbd854e-3f88-4ca3-b86b-a4853adffbd3', 'claim_token': 'd58e68dd-21a6-4a55-8cf4-d7a26bba1264'}},
            {'request': {'status': 'completed'}},
        ]), patch.object(Client, 'sync_all', return_value=summary), \
                patch.object(Client, '_acquire_sync_lock', return_value=True), \
                patch.object(type(self.client.env.cr), 'commit'):
            result = self.client.process_platform_sync_request()

        self.assertTrue(result['accepted'])
        self.assertIsNone(result['error'])
        self.assertEqual(result['summary'], summary)

    def test_full_sync_labels_mapping_issues_as_warnings(self):
        Client = type(self.client)
        methods = (
            'sync_partners', 'sync_products', 'sync_odoo_warehouses',
            'sync_odoo_products', 'sync_odoo_lots', 'sync_odoo_lot_stock',
        )
        patches = [patch.object(Client, name, return_value=0) for name in methods]
        with patch.object(Client, '_is_configured', return_value=True), \
                patch.object(Client, '_acquire_sync_lock', return_value=True), \
                patch.object(Client, 'sync_machines', return_value=(1, [
                    '123 ingredients: solid_1 has no linked Odoo product',
                ])):
            for mocked in patches:
                mocked.start()
            try:
                summary = self.client.sync_all()
            finally:
                for mocked in reversed(patches):
                    mocked.stop()

        self.assertIn('Warnings: machines: 123 ingredients:', summary)
        self.assertNotIn(' Errors:', summary)
