import hashlib
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestSyncRequests(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env['softlife.sync.client']

    def test_processes_platform_request_and_posts_success(self):
        Client = type(self.client)
        committed_pending = []
        with patch.object(Client, '_api_request', side_effect=[
            {'request': {'id': 'd58e68dd-21a6-4a55-8cf4-d7a26bba1264', 'claim_token': '7cbd854e-3f88-4ca3-b86b-a4853adffbd3', 'kind': 'stock_snapshot'}},
            {'request': {'status': 'completed'}},
        ]) as api, patch.object(Client, 'sync_all', return_value='Synced all warehouse stock.'), \
                patch.object(Client, '_acquire_sync_lock', return_value=True), \
                patch.object(type(self.client.env.cr), 'commit') as commit:
            commit.side_effect = lambda: committed_pending.append(
                self.env['ir.config_parameter'].sudo().get_param(
                    self.client._pending_sync_result_key,
                )
            )
            result = self.client.process_platform_sync_request()

        self.assertTrue(result['accepted'])
        self.assertEqual(api.call_args_list[1].args[:2], (
            'POST', '/api/internal/odoo/sync-requests/d58e68dd-21a6-4a55-8cf4-d7a26bba1264/result',
        ))
        self.assertEqual(api.call_args_list[1].kwargs['payload'], result)
        self.assertEqual(result['claim_token'], '7cbd854e-3f88-4ca3-b86b-a4853adffbd3')
        self.assertEqual(commit.call_count, 2)
        self.assertTrue(committed_pending[0])
        self.assertFalse(committed_pending[1])

    def test_reports_partial_sync_errors_as_failure(self):
        Client = type(self.client)
        summary = 'Synced 3 product(s). Errors: odoo_lot_stock: invalid product stock row'
        with patch.object(Client, '_api_request', side_effect=[
            {'request': {'id': '7cbd854e-3f88-4ca3-b86b-a4853adffbd3', 'claim_token': 'd58e68dd-21a6-4a55-8cf4-d7a26bba1264', 'kind': 'stock_snapshot'}},
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
            {'request': {'id': '7cbd854e-3f88-4ca3-b86b-a4853adffbd3', 'claim_token': 'd58e68dd-21a6-4a55-8cf4-d7a26bba1264', 'kind': 'stock_snapshot'}},
            {'request': {'status': 'completed'}},
        ]), patch.object(Client, 'sync_all', return_value=summary), \
                patch.object(Client, '_acquire_sync_lock', return_value=True), \
                patch.object(type(self.client.env.cr), 'commit'):
            result = self.client.process_platform_sync_request()

        self.assertTrue(result['accepted'])
        self.assertIsNone(result['error'])
        self.assertEqual(result['summary'], summary)

    def test_dispatches_remediation_without_full_sync(self):
        Client = type(self.client)
        expected = {'accepted': True, 'summary': 'remediated'}
        payload = {'contract_version': 1}
        payload_hash = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(',', ':'),
        ).encode()).hexdigest()
        with patch.object(Client, 'remediate_fiscal_products', return_value=expected) as remediate, \
                patch.object(Client, 'sync_all') as sync_all:
            result = self.client._dispatch_platform_sync_request({
                'kind': 'fiscal_product_remediation', 'payload': payload,
                'payload_sha256': payload_hash,
            })
        self.assertEqual(result, expected)
        remediate.assert_called_once_with(payload)
        sync_all.assert_not_called()

    def test_rejects_remediation_payload_hash_mismatch(self):
        Client = type(self.client)
        with patch.object(Client, 'sync_all') as sync_all, \
                patch.object(Client, 'remediate_fiscal_products') as remediate:
            result = self.client._dispatch_platform_sync_request({
                'kind': 'fiscal_product_remediation', 'payload': {'contract_version': 1},
                'payload_sha256': '0' * 64,
            })
        self.assertFalse(result['accepted'])
        sync_all.assert_not_called()
        remediate.assert_not_called()

    def test_dispatches_fiscal_invoice_kinds_with_frozen_hashes(self):
        Client = type(self.client)
        payload = {'contract_version': 1}
        payload_hash = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(',', ':'),
        ).encode()).hexdigest()
        with patch.object(Client, '_create_fiscal_invoice_drafts', return_value={
                'accepted': True, 'summary': 'drafted'}) as create, \
                patch.object(Client, '_confirm_fiscal_invoices', return_value={
                    'accepted': True, 'summary': 'confirmed'}) as confirm:
            draft_result = self.client._dispatch_platform_sync_request({
                'kind': 'fiscal_invoice_draft_creation', 'payload': payload,
                'payload_sha256': payload_hash,
            })
            confirmation_result = self.client._dispatch_platform_sync_request({
                'kind': 'fiscal_invoice_bulk_confirmation', 'payload': payload,
                'payload_sha256': payload_hash,
            })
        self.assertEqual(draft_result['summary'], 'drafted')
        self.assertEqual(confirmation_result['summary'], 'confirmed')
        create.assert_called_once_with(payload)
        confirm.assert_called_once_with(payload)
        self.assertFalse(hasattr(self.client, 'create_fiscal_invoice_drafts'))
        self.assertFalse(hasattr(self.client, 'confirm_fiscal_invoices'))

    def test_rejects_fiscal_invoice_hash_mismatch_before_handler(self):
        Client = type(self.client)
        with patch.object(Client, '_create_fiscal_invoice_drafts') as create:
            result = self.client._dispatch_platform_sync_request({
                'kind': 'fiscal_invoice_draft_creation',
                'payload': {'contract_version': 1},
                'payload_sha256': '0' * 64,
            })
        self.assertFalse(result['accepted'])
        create.assert_not_called()

    def test_unknown_request_kind_fails_closed(self):
        Client = type(self.client)
        with patch.object(Client, 'sync_all') as sync_all, \
                patch.object(Client, 'remediate_fiscal_products') as remediate:
            result = self.client._dispatch_platform_sync_request({'kind': 'future_operation'})
        self.assertFalse(result['accepted'])
        sync_all.assert_not_called()
        remediate.assert_not_called()

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
