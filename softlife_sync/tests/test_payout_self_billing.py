import hashlib
import json
import uuid
from unittest.mock import Mock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from ..models.softlife_sync_client import SoftlifeAPIError


class TestPayoutSelfBilling(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env['softlife.sync.client']

    @staticmethod
    def _hash(payload):
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode()).hexdigest()

    def _configuration_and_payload(self):
        company = self.env.company
        country = company.account_fiscal_country_id
        if not country or country.code != 'ES' or company.currency_id.name != 'EUR':
            self.skipTest('Test company is not a Spanish EUR accounting company.')
        state = self.env['res.country.state'].search([('country_id', '=', country.id)], limit=1)
        if not state:
            self.skipTest('Spanish payout tests require one configured province.')
        company.with_context(no_vat_validation=True).write({
            'vat': company.vat or 'B99286320', 'street': company.street or 'Calle SoftLife 1',
            'zip': company.zip or '28001', 'city': company.city or 'Madrid',
            'state_id': company.state_id.id or state.id, 'country_id': country.id,
        })
        source_account = self.env['account.account'].with_company(company).search([
            ('account_type', 'in', ('expense', 'expense_direct_cost')),
        ], limit=1)
        if not source_account:
            self.skipTest('Test company has no expense account.')
        account = self.env['account.account'].with_company(company).search([
            ('code', '=', '623100'),
        ]).filtered(lambda candidate: self.client._account_applies_to_company(
            candidate, company
        ))
        if not account:
            account = source_account.copy({'name': 'Comisiones / Revenue Share Ubicaciones',
                                           'code': '623100'})
        journal = self.env['account.journal'].with_company(company).search([
            ('code', '=', 'AFRS'), ('type', '=', 'purchase'), ('company_id', '=', company.id),
        ])
        if not journal:
            journal = self.env['account.journal'].with_company(company).create({
                'name': 'Autofacturas - Revenue Share', 'code': 'AFRS',
                'type': 'purchase', 'company_id': company.id,
            })
        if 'is_self_billing' in journal._fields:
            journal.is_self_billing = True
        account_values = {'currency_id': False}
        if 'deprecated' in account._fields:
            account_values['deprecated'] = False
        if 'allowed_journal_ids' in account._fields:
            account_values['allowed_journal_ids'] = [(5, 0, 0)]
        account.write(account_values)
        if 'account_control_ids' in journal._fields:
            journal.account_control_ids = [(5, 0, 0)]
        self.env['ir.config_parameter'].sudo().set_param(
            'softlife.sync.fiscal_company_id', company.id
        )
        tax = self.env.ref(
            f'account.{company.id}_account_tax_template_p_iva21_sc',
            raise_if_not_found=False,
        )
        if not tax:
            self.skipTest('Test company has no standard Spanish domestic 21% service purchase tax.')
        tax.write({'price_include_override': 'tax_excluded', 'active': True})
        bill = {
            'platform_bill_id': str(uuid.uuid4()),
            'settlement_id': str(uuid.uuid4()),
            'tenant_id': str(uuid.uuid4()),
            'period_month': '2026-09', 'invoice_date': '2026-10-01',
            'currency': 'EUR', 'move_type': 'in_invoice',
            'reference': None,
            'description': 'Servicios de gestión, facturación por el destinatario',
            'gross_cents': 1210, 'tax_base_cents': 1000, 'iva_cents': 210,
            'iva_rate_percent': 21, 'contract_acceptance_id': str(uuid.uuid4()),
            'contract_version': 'SL-GESTOR-2026-10-27-v4',
            'source_snapshot_sha256': 'a' * 64,
            'vendor': {
                'legal_name': 'Gestor Payout SL', 'tax_id': 'B99286320',
                'email': 'gestor@example.com', 'address_line_1': 'Calle Mayor 1',
                'address_line_2': None, 'postal_code': '28001', 'city': 'Madrid',
                'province': state.name.swapcase(), 'country_code': 'ES',
            },
        }
        partner = self.env['res.partner'].with_context(no_vat_validation=True).create({
            'name': bill['vendor']['legal_name'], 'vat': bill['vendor']['tax_id'],
            'email': bill['vendor']['email'], 'street': bill['vendor']['address_line_1'],
            'zip': bill['vendor']['postal_code'], 'city': bill['vendor']['city'],
            'state_id': state.id, 'country_id': country.id, 'company_type': 'company',
            'is_company': True, 'supabase_id': bill['tenant_id'],
        })
        controlled_accounts = account | tax.invoice_repartition_line_ids.account_id \
            | partner.with_company(company).property_account_payable_id
        for controlled_account in controlled_accounts:
            values = {'currency_id': False}
            if 'deprecated' in controlled_account._fields:
                values['deprecated'] = False
            if 'allowed_journal_ids' in controlled_account._fields:
                values['allowed_journal_ids'] = [(5, 0, 0)]
            controlled_account.write(values)
        bill['reference'] = f'AUTOFAC-{bill["period_month"]}-{bill["tenant_id"]}'
        payload = {'schema_version': 1, 'bill': bill}
        return company, journal, account, tax, payload, self._hash(payload)

    def _draft(self):
        records = self._configuration_and_payload()
        Client = type(self.client)
        with patch.object(Client, '_payout_enabled', return_value=True), \
                patch.object(Client, '_assert_payout_date_unlocked'):
            result = self.client._create_payout_vendor_bill_draft(records[4], records[5])
        return (*records, result)

    def test_configuration_uses_standard_domestic_service_tax(self):
        company, _journal, _account, tax, _payload, _payload_hash = \
            self._configuration_and_payload()
        tax.copy({'name': 'Another excluded 21% purchase tax', 'active': True})

        configured = self.client._payout_configuration()

        self.assertEqual(configured[0], company)
        self.assertEqual(configured[3], tax)

    def test_draft_accepts_explicitly_missing_contract_acceptance(self):
        company, journal, account, tax, payload, payload_hash = \
            self._configuration_and_payload()
        payload['bill']['contract_acceptance_id'] = None
        payload_hash = self._hash(payload)

        bill, configured_company, configured_journal, configured_account, configured_tax, _, _ = \
            self.client._validate_payout_draft_payload(payload, payload_hash)

        self.assertIsNone(bill['contract_acceptance_id'])
        self.assertEqual((configured_company, configured_journal, configured_account, configured_tax),
                         (company, journal, account, tax))

    def test_draft_accepts_incomplete_customer_supplied_vendor_details(self):
        _company, _journal, _account, _tax, payload, _payload_hash = \
            self._configuration_and_payload()
        payload['bill']['vendor'].update({
            'address_line_1': '', 'postal_code': '', 'city': '',
            'province': '', 'country_code': '',
        })

        bill, *_ = self.client._validate_payout_draft_payload(payload, self._hash(payload))

        self.assertEqual(bill['vendor']['address_line_1'], '')

    def test_creates_exact_draft_and_reuses_it(self):
        company, journal, account, tax, payload, payload_hash = \
            self._configuration_and_payload()
        Client = type(self.client)
        with patch.object(Client, '_payout_enabled', return_value=True), \
                patch.object(Client, '_assert_payout_date_unlocked'):
            first = self.client._create_payout_vendor_bill_draft(payload, payload_hash)
            second = self.client._create_payout_vendor_bill_draft(payload, payload_hash)
        expected_keys = {'accepted', 'bill'}
        self.assertEqual(set(first), expected_keys)
        self.assertEqual(set(first['bill']), {
            'platform_bill_id', 'odoo_move_id', 'odoo_partner_id', 'odoo_journal_id',
            'payload_sha256', 'state', 'created', 'amount_total_cents',
        })
        self.assertTrue(first['bill']['created'])
        self.assertFalse(second['bill']['created'])
        self.assertEqual(self.env['res.partner'].search_count([
            ('supabase_id', '=', payload['bill']['tenant_id']),
        ]), 1)
        move = self.env['account.move'].browse(first['bill']['odoo_move_id'])
        self.assertEqual(move.state, 'draft')
        self.assertEqual(move.move_type, 'in_invoice')
        self.assertEqual(move.journal_id, journal)
        self.assertFalse(move.invoice_line_ids.product_id)
        self.assertEqual(move.invoice_line_ids.account_id, account)
        self.assertEqual(move.invoice_line_ids.tax_ids, tax)
        self.assertEqual(move.invoice_line_ids.price_unit, 10.00)
        self.assertEqual(self.client._amount_cents(move.amount_total), 1210)
        self.assertEqual(self.client._amount_cents(move.amount_untaxed), 1000)
        self.assertEqual(self.client._amount_cents(move.amount_tax), 210)
        self.assertEqual(move.partner_id.supabase_id, payload['bill']['tenant_id'])
        self.assertEqual(move.invoice_date.isoformat(), '2026-10-01')
        self.assertEqual(move.invoice_date_due.isoformat(), '2026-10-15')
        self.assertFalse(move.invoice_payment_term_id)
        self.assertEqual(move.softlife_payout_recipient_snapshot['tax_id'], company.vat)

    def test_rejects_wrong_invoice_date_and_incorrect_tax_math(self):
        company, journal, account, tax, payload, payload_hash = \
            self._configuration_and_payload()
        payload['bill']['invoice_date'] = '2026-09-30'
        with self.assertRaisesRegex(SoftlifeAPIError, 'values are invalid'):
            self.client._validate_payout_draft_payload(payload, self._hash(payload))
        payload['bill']['invoice_date'] = '2026-10-01'
        payload['bill'].update({'gross_cents': 1210, 'tax_base_cents': 1210, 'iva_cents': 254})
        with self.assertRaisesRegex(SoftlifeAPIError, 'tax-base IVA'):
            self.client._validate_payout_draft_payload(payload, self._hash(payload))

    def test_draft_is_frozen_and_manual_post_is_blocked(self):
        company, journal, account, tax, payload, payload_hash, result = self._draft()
        move = self.env['account.move'].browse(result['bill']['odoo_move_id'])
        with self.assertRaisesRegex(UserError, 'headers are frozen'):
            move.write({'ref': 'Manual change'})
        with self.assertRaisesRegex(UserError, 'headers are frozen'):
            move.write({'invoice_date_due': '2026-10-16'})
        with self.assertRaisesRegex(UserError, 'lines are frozen'):
            move.invoice_line_ids.write({'name': 'Manual change'})
        with self.assertRaisesRegex(UserError, 'validated payout handler'):
            move.action_post()

    def test_posts_once_and_retries_idempotently(self):
        company, journal, account, tax, payload, payload_hash, draft = self._draft()
        posting = {'schema_version': 1, 'bill': {
            'platform_bill_id': payload['bill']['platform_bill_id'],
            'odoo_move_id': draft['bill']['odoo_move_id'],
            'payload_sha256': payload_hash,
        }}
        Client = type(self.client)
        with patch.object(Client, '_payout_enabled', return_value=True), \
                patch.object(Client, '_assert_payout_date_unlocked'):
            first = self.client._post_payout_vendor_bill(posting)
            second = self.client._post_payout_vendor_bill(posting)
        self.assertTrue(first['bill']['posted'])
        self.assertFalse(second['bill']['posted'])
        self.assertEqual(first['bill']['state'], 'posted')
        self.assertTrue(first['bill']['name'])
        move = self.env['account.move'].browse(draft['bill']['odoo_move_id'])
        self.assertEqual(move.journal_id.code, 'AFRS')
        self.assertEqual(move.invoice_date_due.isoformat(), '2026-10-15')
        self.assertEqual(set(first['bill']), {
            'platform_bill_id', 'odoo_move_id', 'payload_sha256', 'state', 'name', 'posted',
        })

    def test_pdf_upload_is_raw_and_attachment_is_reused(self):
        company, journal, account, tax, payload, payload_hash, draft = self._draft()
        move = self.env['account.move'].browse(draft['bill']['odoo_move_id'])
        Client = type(self.client)
        with patch.object(Client, '_payout_enabled', return_value=True), \
                patch.object(Client, '_assert_payout_date_unlocked'):
            posted = self.client._post_payout_vendor_bill({'schema_version': 1, 'bill': {
                'platform_bill_id': payload['bill']['platform_bill_id'],
                'odoo_move_id': move.id, 'payload_sha256': payload_hash,
            }})
        pdf_payload = {'schema_version': 1, 'bill': {
            'platform_bill_id': payload['bill']['platform_bill_id'],
            'odoo_move_id': move.id, 'payload_sha256': payload_hash,
            'odoo_name': posted['bill']['name'],
        }}
        self.assertEqual(
            self.client._payout_invoice_report(), self.env.ref('account.account_invoices')
        )
        report = Mock(report_name='account.report_invoice')
        report._render_qweb_pdf.return_value = (b'%PDF-1.7\nself-bill', 'pdf')
        request = {'id': str(uuid.uuid4()), 'claim_token': str(uuid.uuid4())}
        with patch.object(Client, '_payout_enabled', return_value=True), \
                patch.object(Client, '_payout_invoice_report', return_value=report), \
                patch.object(Client, '_api_binary_post') as upload:
            first = self.client._export_payout_vendor_bill_pdf(pdf_payload, request)
            second = self.client._export_payout_vendor_bill_pdf(pdf_payload, request)
        self.assertEqual(first, second)
        self.assertEqual(report._render_qweb_pdf.call_count, 1)
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(upload.call_args.args[1], b'%PDF-1.7\nself-bill')
        headers = upload.call_args.kwargs['headers']
        self.assertEqual(headers['x-odoo-request-id'], request['id'])
        self.assertEqual(headers['x-odoo-claim-token'], request['claim_token'])
        self.assertEqual(headers['x-odoo-move-id'], str(move.id))
        self.assertEqual(headers['x-odoo-attachment-id'], str(first['bill']['odoo_attachment_id']))
        self.assertEqual(headers['x-pdf-sha256'], first['bill']['pdf_sha256'])

    def test_opt_ins_are_independent_and_default_closed(self):
        Client = type(self.client)
        with patch.object(Client, '_payout_enabled', return_value=False):
            with self.assertRaisesRegex(SoftlifeAPIError, 'draft creation is disabled'):
                self.client._create_payout_vendor_bill_draft({}, 'a' * 64)
            with self.assertRaisesRegex(SoftlifeAPIError, 'posting is disabled'):
                self.client._post_payout_vendor_bill({})
            with self.assertRaisesRegex(SoftlifeAPIError, 'PDF export is disabled'):
                self.client._export_payout_vendor_bill_pdf({}, {})

    def test_settings_require_existing_afrs_configuration(self):
        settings = self.env['res.config.settings'].create({
            'softlife_payout_bill_draft_creation_enabled': True,
        })
        Client = type(self.client)
        with patch.object(Client, '_payout_configuration', side_effect=SoftlifeAPIError(
                'Odoo must contain exactly one active AFRS purchase journal.')):
            with self.assertRaisesRegex(UserError, 'AFRS purchase journal'):
                settings.set_values()
