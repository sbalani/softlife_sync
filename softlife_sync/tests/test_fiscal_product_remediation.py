from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..models.softlife_sync_client import SoftlifeAPIError


class TestFiscalProductRemediation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env['softlife.sync.client']

    def test_requires_independent_opt_in_before_platform_call(self):
        Client = type(self.client)
        with patch.object(Client, '_fiscal_product_remediation_enabled', return_value=False), \
                patch.object(Client, '_api_request') as api:
            with self.assertRaisesRegex(SoftlifeAPIError, 'disabled in Odoo Settings'):
                self.client.remediate_fiscal_products({})
        api.assert_not_called()

    def test_rejects_duplicate_products_before_record_resolution(self):
        company = self.env.company
        self.env['ir.config_parameter'].sudo().set_param(
            'softlife.sync.fiscal_company_id', str(company.id),
        )
        payload = {
            'contract_version': 1,
            'configuration_report_id': 'report-id',
            'configuration_payload_sha256': 'a' * 64,
            'company': {
                'odoo_id': company.id,
                'country_code': company.account_fiscal_country_id.code,
                'currency': company.currency_id.name,
            },
            'customer': {'odoo_id': self.env.user.partner_id.id},
            'target': {
                'income_account': {'odoo_id': 1, 'code': '700000', 'account_type': 'income'},
                'sale_tax': {
                    'odoo_tax_id': 1, 'rate': 10, 'country_code': 'ES',
                    'type_tax_use': 'sale', 'amount_type': 'percent', 'price_include': True,
                },
            },
            'products': [
                {'odoo_product_id': 7, 'remediate_income_account': True,
                 'remediate_customer_taxes': False},
                {'odoo_product_id': 7, 'remediate_income_account': False,
                 'remediate_customer_taxes': True},
            ],
        }
        contract = {
            'contract_version': 1,
            'expected': {'customer_odoo_id': self.env.user.partner_id.id},
        }
        with self.assertRaisesRegex(SoftlifeAPIError, 'product entry is invalid'):
            self.client._validate_fiscal_product_remediation(payload, contract)

    def test_payload_schema_is_exact(self):
        payload = {
            'contract_version': 1,
            'configuration_report_id': 'report-id',
            'configuration_payload_sha256': 'a' * 64,
            'company': {}, 'customer': {}, 'target': {}, 'products': [],
            'unexpected': True,
        }
        with self.assertRaisesRegex(SoftlifeAPIError, 'payload fields are invalid'):
            self.client._validate_fiscal_product_remediation(payload, {})

    def _spanish_remediation_records(self):
        company = self.env.company
        country = company.account_fiscal_country_id
        if not country or country.code != 'ES':
            self.skipTest('Test company does not have Spanish fiscal localization.')
        account = self.env['account.account'].with_company(company).search([
            ('account_type', 'in', ('income', 'income_other')),
        ], limit=1)
        if not account:
            self.skipTest('Test company has no income account.')
        customer = self.env['res.partner'].create({
            'name': 'Fiscal remediation customer', 'country_id': country.id,
        })
        tax = self.env['account.tax'].with_company(company).create({
            'name': 'Fiscal remediation 12.345%', 'amount': 12.345,
            'amount_type': 'percent', 'type_tax_use': 'sale',
            'company_id': company.id, 'country_id': country.id, 'price_include': True,
        })
        old_tax = self.env['account.tax'].with_company(company).create({
            'name': 'Fiscal remediation old 8.765%', 'amount': 8.765,
            'amount_type': 'percent', 'type_tax_use': 'sale',
            'company_id': company.id, 'country_id': country.id, 'price_include': False,
        })
        category = self.env['product.category'].with_company(company).create({
            'name': 'Fiscal remediation category',
        })
        target = self.env['product.product'].with_company(company).create({
            'name': 'Fiscal remediation target', 'categ_id': category.id,
            'softlife_recipe_id': 'fiscal-remediation-target', 'taxes_id': [(6, 0, old_tax.ids)],
        })
        unrelated = self.env['product.product'].with_company(company).create({
            'name': 'Fiscal remediation unrelated', 'categ_id': category.id,
            'softlife_recipe_id': 'fiscal-remediation-unrelated', 'taxes_id': [(6, 0, old_tax.ids)],
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'softlife.sync.fiscal_company_id', str(company.id),
        )
        contract = {
            'contract_version': 1,
            'expected': {
                'journal_code': 'VEND', 'customer_odoo_id': customer.id,
                'vat_rate': tax.amount, 'currency': company.currency_id.name,
                'income_account_code': account.code,
            },
        }
        payload = {
            'contract_version': 1,
            'configuration_report_id': 'fiscal-report-id',
            'configuration_payload_sha256': 'b' * 64,
            'company': {
                'odoo_id': company.id, 'country_code': country.code,
                'currency': company.currency_id.name,
            },
            'customer': {'odoo_id': customer.id},
            'target': {
                'income_account': {
                    'odoo_id': account.id, 'code': account.code,
                    'account_type': account.account_type,
                },
                'sale_tax': {
                    'odoo_tax_id': tax.id, 'rate': tax.amount, 'country_code': 'ES',
                    'type_tax_use': 'sale', 'amount_type': 'percent', 'price_include': True,
                },
            },
            'products': [{
                'odoo_product_id': target.id, 'remediate_income_account': True,
                'remediate_customer_taxes': True,
            }],
        }
        return company, account, tax, old_tax, target, unrelated, contract, payload

    def test_targeted_writes_are_idempotent_and_do_not_create_invoices(self):
        company, account, tax, old_tax, target, unrelated, contract, payload = \
            self._spanish_remediation_records()
        Client = type(self.client)
        invoice_count = self.env['account.move'].search_count([])
        with patch.object(Client, '_fiscal_product_remediation_enabled', return_value=True), \
                patch.object(Client, '_fiscal_configuration_payload', return_value={'fresh': True}), \
                patch.object(Client, '_api_request', side_effect=[
                    contract, {'id': 'verification-1', 'payload_sha256': 'c' * 64},
                    contract, {'id': 'verification-2', 'payload_sha256': 'd' * 64},
                ]):
            first = self.client.remediate_fiscal_products(payload)
            second = self.client.remediate_fiscal_products(payload)

        effective_account, effective_taxes = self.client._effective_product_fiscal_configuration(
            target, company, False,
        )
        self.assertEqual(effective_account, account)
        self.assertEqual(effective_taxes, tax)
        self.assertEqual(unrelated.taxes_id._filter_taxes_by_company(company), old_tax)
        self.assertFalse(unrelated.with_company(company).property_account_income_id)
        self.assertEqual(first['verification_report_id'], 'verification-1')
        self.assertEqual(second['verification_report_id'], 'verification-2')
        self.assertEqual(self.env['account.move'].search_count([]), invoice_count)

    def test_invalid_batch_writes_nothing(self):
        company, account, tax, old_tax, target, unrelated, contract, payload = \
            self._spanish_remediation_records()
        payload['products'].append({
            'odoo_product_id': 2147483647, 'remediate_income_account': True,
            'remediate_customer_taxes': False,
        })
        Client = type(self.client)
        with patch.object(Client, '_fiscal_product_remediation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            with self.assertRaisesRegex(SoftlifeAPIError, 'inactive or non-SoftLife product'):
                self.client.remediate_fiscal_products(payload)
        self.assertEqual(target.taxes_id._filter_taxes_by_company(company), old_tax)
        self.assertFalse(target.with_company(company).property_account_income_id)

    def test_preserves_tax_from_another_company(self):
        company, account, tax, old_tax, target, unrelated, contract, payload = \
            self._spanish_remediation_records()
        other_company = self.env['res.company'].create({
            'name': 'Fiscal remediation other company',
            'country_id': company.country_id.id,
            'currency_id': company.currency_id.id,
        })
        other_tax = self.env['account.tax'].sudo().with_company(other_company).create({
            'name': 'Other company retained tax', 'amount': 3.21,
            'amount_type': 'percent', 'type_tax_use': 'sale',
            'company_id': other_company.id, 'country_id': company.account_fiscal_country_id.id,
        })
        all_companies = [company.id, other_company.id]
        target.product_tmpl_id.sudo().with_context(allowed_company_ids=all_companies).write({
            'taxes_id': [(6, 0, old_tax.ids + other_tax.ids)],
        })
        Client = type(self.client)
        with patch.object(Client, '_fiscal_product_remediation_enabled', return_value=True), \
                patch.object(Client, '_fiscal_configuration_payload', return_value={'fresh': True}), \
                patch.object(Client, '_api_request', side_effect=[contract, {'report_id': 'verified'}]):
            self.client.remediate_fiscal_products(payload)
        resulting_ids = set(target.product_tmpl_id.sudo().with_context(
            active_test=False, allowed_company_ids=all_companies,
        ).taxes_id.ids)
        self.assertEqual(resulting_ids, {tax.id, other_tax.id})

    def test_effective_fiscal_position_verification_rolls_back(self):
        company, account, tax, old_tax, target, unrelated, contract, payload = \
            self._spanish_remediation_records()
        mapped_account = self.env['account.account'].with_company(company).search([
            ('account_type', 'in', ('income', 'income_other')),
            ('id', '!=', account.id),
        ], limit=1)
        if not mapped_account:
            self.skipTest('Test company has only one income account.')
        fiscal_position = self.env['account.fiscal.position'].with_company(company).create({
            'name': 'Fiscal remediation verification mapping', 'company_id': company.id,
            'account_ids': [(0, 0, {
                'account_src_id': account.id, 'account_dest_id': mapped_account.id,
            })],
        })
        self.env['res.partner'].browse(payload['customer']['odoo_id']).write({
            'property_account_position_id': fiscal_position.id,
        })
        Client = type(self.client)
        with patch.object(Client, '_fiscal_product_remediation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            with self.assertRaisesRegex(SoftlifeAPIError, 'account verification failed'):
                self.client.remediate_fiscal_products(payload)
        self.assertEqual(target.taxes_id._filter_taxes_by_company(company), old_tax)
        self.assertFalse(target.with_company(company).property_account_income_id)

    def test_rejects_partial_multi_variant_template(self):
        company, account, tax, old_tax, target, unrelated, contract, payload = \
            self._spanish_remediation_records()
        attribute = self.env['product.attribute'].create({'name': 'Remediation size'})
        values = self.env['product.attribute.value'].create([
            {'name': 'Remediation small', 'attribute_id': attribute.id},
            {'name': 'Remediation large', 'attribute_id': attribute.id},
        ])
        template = self.env['product.template'].with_company(company).create({
            'name': 'Fiscal remediation variants',
            'taxes_id': [(6, 0, old_tax.ids)],
            'attribute_line_ids': [(0, 0, {
                'attribute_id': attribute.id, 'value_ids': [(6, 0, values.ids)],
            })],
        })
        variants = template.product_variant_ids
        for index, variant in enumerate(variants):
            variant.softlife_recipe_id = f'fiscal-remediation-variant-{index}'
        payload['products'] = [{
            'odoo_product_id': variants[0].id, 'remediate_income_account': True,
            'remediate_customer_taxes': False,
        }]
        Client = type(self.client)
        with patch.object(Client, '_fiscal_product_remediation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            with self.assertRaisesRegex(SoftlifeAPIError, 'include every variant'):
                self.client.remediate_fiscal_products(payload)
