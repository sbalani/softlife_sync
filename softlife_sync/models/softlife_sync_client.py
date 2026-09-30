"""Connector: pulls operational data from the SoftLife platform (Supabase REST)
into Odoo. Odoo is the downstream ERP; the middleware is the system of record.
"""
import copy
import datetime
import hashlib
import json
import logging
import math
import re
import uuid
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .account_move import _softlife_fiscal_internal_scope

_logger = logging.getLogger(__name__)


class SoftlifeAPIError(UserError):
    """A platform HTTP failure with enough detail for a retry decision."""

    def __init__(self, message, status=None, code=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


class SoftlifeSyncClient(models.TransientModel):
    _name = 'softlife.sync.client'
    _description = 'SoftLife Platform Sync (Supabase connector)'
    _sync_lock_id = 731904628
    _pending_sync_result_key = 'softlife.sync.pending_platform_request_result'

    # ------------------------------------------------------------------
    # Config / HTTP
    # ------------------------------------------------------------------
    @api.model
    def _param(self, key, default=False):
        return self.env['ir.config_parameter'].sudo().get_param(key, default)

    @api.model
    def _is_configured(self):
        return bool(
            self._param('softlife.sync.supabase_url')
            and self._param('softlife.sync.supabase_key')
        )

    @api.model
    def _api_is_configured(self):
        return bool(
            self._param('softlife.sync.platform_url')
            and self._param('softlife.sync.odoo_sync_secret')
        )

    @api.model
    def _api_request(self, method, path, params=None, payload=None):
        import requests
        base = (self._param('softlife.sync.platform_url') or '').strip().rstrip('/')
        secret = self._param('softlife.sync.odoo_sync_secret')
        if not base or not secret:
            raise SoftlifeAPIError(_('Platform App URL / Odoo sync secret not configured.'), code='not_configured')
        url = urljoin(f'{base}/', path.lstrip('/'))
        try:
            response = requests.request(
                method, url, params=params, json=payload,
                headers={'x-odoo-sync-secret': secret, 'Accept': 'application/json'},
                timeout=(10, 60),
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise SoftlifeAPIError(
                _('Platform request failed: %s') % exc, code='network_error', retryable=True,
            ) from exc
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if not 200 <= response.status_code < 300:
            message = body.get('error') if isinstance(body, dict) else None
            code = body.get('code') if isinstance(body, dict) else None
            raise SoftlifeAPIError(
                _('Platform %s %s failed (%s): %s') % (
                    method, path, response.status_code, message or response.text[:500],
                ),
                status=response.status_code, code=code,
                retryable=response.status_code in (408, 425, 429) or response.status_code >= 500,
            )
        if not isinstance(body, dict):
            raise SoftlifeAPIError(_('Platform returned an invalid JSON object.'), code='invalid_response')
        return body

    @api.model
    def _api_catalog_pages(self):
        cursor = None
        while True:
            params = {'limit': 500}
            if cursor:
                params['cursor'] = cursor
            page = self._api_request('GET', '/api/internal/odoo/catalog', params=params)
            yield page
            if not page.get('has_more'):
                break
            cursor = page.get('next_cursor')
            if not cursor:
                raise SoftlifeAPIError(_('Catalog pagination omitted next_cursor.'), code='invalid_response')

    @api.model
    def _fiscal_configuration_payload(self, contract):
        if contract.get('contract_version') != 1 or not isinstance(contract.get('expected'), dict):
            raise SoftlifeAPIError(_('Platform returned an unsupported fiscal contract.'), code='invalid_response')
        expected = contract['expected']
        required = ('journal_code', 'customer_odoo_id', 'vat_rate', 'currency', 'income_account_code')
        if any(expected.get(field) in (None, '') for field in required):
            raise SoftlifeAPIError(_('Platform fiscal contract omitted required settings.'), code='invalid_response')

        company = self._fiscal_company()
        self = self.sudo().with_company(company)
        fiscal_country = company.account_fiscal_country_id
        income_accounts = self.env['account.account'].with_company(company).search([
            ('code', '=', str(expected['income_account_code'])),
            ('account_type', 'in', ('income', 'income_other')),
        ])
        income_accounts = income_accounts.filtered(
            lambda account: self._account_applies_to_company(account, company)
        )
        income_account = income_accounts if len(income_accounts) == 1 else self.env['account.account']
        journal = self.env['account.journal'].with_company(company).with_context(active_test=False).search([
            ('company_id', '=', company.id),
            ('code', '=', str(expected['journal_code'])),
        ], limit=1)
        customer_id = expected['customer_odoo_id']
        if not isinstance(customer_id, int) or isinstance(customer_id, bool) or customer_id <= 0:
            raise SoftlifeAPIError(_('Platform fiscal customer ID is invalid.'), code='invalid_response')
        customer = self.env['res.partner'].browse(customer_id).exists().with_company(company)
        try:
            expected_rate = float(expected['vat_rate'])
        except (TypeError, ValueError) as exc:
            raise SoftlifeAPIError(_('Platform fiscal VAT rate is invalid.'), code='invalid_response') from exc
        if not math.isfinite(expected_rate) or expected_rate <= 0:
            raise SoftlifeAPIError(_('Platform fiscal VAT rate is invalid.'), code='invalid_response')
        tax = self.env['account.tax'].with_company(company).search([
            ('company_id', '=', company.id),
            ('type_tax_use', '=', 'sale'),
            ('amount_type', '=', 'percent'),
            ('amount', '=', expected_rate),
            ('country_id', '=', fiscal_country.id),
        ], order='id', limit=1)

        products = self.env['product.product'].with_company(company).search([
            ('active', '=', True),
            ('softlife_recipe_id', '!=', False),
        ], order='id')
        fiscal_position = customer and self.env['account.fiscal.position'].with_company(company)._get_fiscal_position(customer)
        product_rows = []
        for product in products:
            income, taxes = self._effective_product_fiscal_configuration(
                product, company, fiscal_position,
            )
            rates = sorted(set(
                tax_row.amount for tax_row in taxes
                if tax_row.type_tax_use == 'sale' and tax_row.amount_type == 'percent'
            ))
            product_rows.append({
                'odoo_product_id': product.id,
                'sale_ok': bool(product.sale_ok),
                'income_account_code': income.code if income else None,
                'sale_tax_rates': rates,
                'sale_tax_country_codes': sorted(set(
                    tax_row.country_id.code for tax_row in taxes if tax_row.country_id.code
                )),
                'sale_tax_ids': sorted(taxes.ids),
                'sale_taxes': [{
                    'odoo_tax_id': tax_row.id,
                    'rate': tax_row.amount,
                    'country_code': tax_row.country_id.code if tax_row.country_id else None,
                    'price_include': bool(tax_row.price_include),
                    'amount_type': tax_row.amount_type,
                    'type_tax_use': tax_row.type_tax_use,
                } for tax_row in taxes.sorted('id')],
            })

        return {
            'contract_version': 1,
            'capabilities': {
                'fiscal_product_remediation': 1,
                'fiscal_invoice_draft_creation': 1,
                'fiscal_invoice_bulk_confirmation': 1,
                'fiscal_zero_value_invoices': 1,
            },
            'checked_at': fields.Datetime.now().isoformat() + 'Z',
            'company': {
                'odoo_id': company.id,
                'country_code': fiscal_country.code if fiscal_country else None,
                'vat': company.vat or None,
                'currency': company.currency_id.name or None,
            },
            'income_account': {
                'odoo_id': income_account.id if income_account else None,
                'code': income_account.code if income_account else None,
                'account_type': income_account.account_type if income_account else None,
            },
            'journal': {
                'code': journal.code if journal else None,
                'type': journal.type if journal else None,
                'refund_sequence': bool(journal and journal.refund_sequence),
                'secure_posted_entries': bool(journal and journal.restrict_mode_hash_table),
            },
            'customer': {
                'odoo_id': customer.id if customer else None,
                'country_code': customer.country_id.code if customer and customer.country_id else None,
                'vat': (customer.vat or None) if customer else None,
            },
            'tax': {
                'odoo_id': tax.id if tax else None,
                'type_tax_use': tax.type_tax_use if tax else None,
                'rate': tax.amount if tax else None,
                'country_code': tax.country_id.code if tax and tax.country_id else None,
                'price_include': bool(tax and tax.price_include),
                'amount_type': tax.amount_type if tax else None,
            },
            'products': product_rows,
        }

    @api.model
    def _fiscal_company(self):
        raw_company_id = self._param('softlife.sync.fiscal_company_id')
        try:
            company_id = int(raw_company_id)
        except (TypeError, ValueError) as exc:
            raise SoftlifeAPIError(_(
                'Select the Fiscal issuing company in SoftLife Sync settings.'
            ), code='fiscal_company_not_configured') from exc
        company = self.env['res.company'].sudo().browse(company_id).exists()
        if not company or not company.active:
            raise SoftlifeAPIError(_(
                'The configured Fiscal issuing company is missing or inactive.'
            ), code='fiscal_company_not_configured')
        return company

    @api.model
    def _account_applies_to_company(self, account, company):
        if 'company_ids' in account._fields:
            return company in account.company_ids
        if 'company_id' in account._fields:
            return account.company_id == company
        return True

    @api.model
    def _effective_product_fiscal_configuration(self, product, company, fiscal_position):
        product = product.with_company(company)
        income = product.product_tmpl_id.with_company(company).get_product_accounts(
            fiscal_pos=fiscal_position,
        ).get('income')
        taxes = product.taxes_id._filter_taxes_by_company(company)
        if fiscal_position:
            taxes = fiscal_position.map_tax(taxes)
        return income, taxes

    @api.model
    def report_fiscal_configuration(self):
        if not self._fiscal_configuration_reporting_enabled():
            return {'accepted': False, 'disabled': True}
        contract = self._api_request('GET', '/api/internal/odoo/fiscal-configuration')
        payload = self._fiscal_configuration_payload(contract)
        return self._api_request(
            'POST', '/api/internal/odoo/fiscal-configuration', payload=payload,
        )

    @api.model
    def _fiscal_configuration_reporting_enabled(self):
        return str(self._param('softlife.sync.fiscal_reporting_enabled', 'False')).lower() in ('1', 'true')

    @api.model
    def _fiscal_product_remediation_enabled(self):
        return str(self._param(
            'softlife.sync.fiscal_product_remediation_enabled', 'False',
        )).lower() in ('1', 'true')

    @api.model
    def _fiscal_invoice_draft_creation_enabled(self):
        return str(self._param(
            'softlife.sync.fiscal_invoice_draft_creation_enabled', 'False',
        )).lower() in ('1', 'true')

    @api.model
    def _fiscal_invoice_confirmation_enabled(self):
        return str(self._param(
            'softlife.sync.fiscal_invoice_confirmation_enabled', 'False',
        )).lower() in ('1', 'true')

    @api.model
    def _remediation_error(self, message):
        raise SoftlifeAPIError(message, code='invalid_fiscal_product_remediation')

    @api.model
    def _validate_fiscal_product_remediation(self, payload, contract):
        expected_keys = {
            'contract_version', 'configuration_report_id', 'configuration_payload_sha256',
            'company', 'customer', 'target', 'products',
        }
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            self._remediation_error(_('Fiscal product remediation payload fields are invalid.'))
        if payload['contract_version'] != 1:
            self._remediation_error(_('Fiscal product remediation contract version is unsupported.'))
        report_id = payload['configuration_report_id']
        digest = payload['configuration_payload_sha256']
        if not isinstance(report_id, str) or not report_id.strip():
            self._remediation_error(_('Fiscal product remediation report ID is invalid.'))
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            self._remediation_error(_('Fiscal product remediation report hash is invalid.'))

        company_payload = payload['company']
        customer_payload = payload['customer']
        target = payload['target']
        products_payload = payload['products']
        if not isinstance(company_payload, dict) or set(company_payload) != {
                'odoo_id', 'country_code', 'currency'}:
            self._remediation_error(_('Fiscal product remediation company fields are invalid.'))
        if not isinstance(customer_payload, dict) or set(customer_payload) != {'odoo_id'}:
            self._remediation_error(_('Fiscal product remediation customer fields are invalid.'))
        if not isinstance(target, dict) or set(target) != {'income_account', 'sale_tax'}:
            self._remediation_error(_('Fiscal product remediation target fields are invalid.'))
        account_payload = target['income_account']
        tax_payload = target['sale_tax']
        if not isinstance(account_payload, dict) or set(account_payload) != {
                'odoo_id', 'code', 'account_type'}:
            self._remediation_error(_('Fiscal product remediation income account fields are invalid.'))
        if not isinstance(tax_payload, dict) or set(tax_payload) != {
                'odoo_tax_id', 'rate', 'country_code', 'type_tax_use', 'amount_type',
                'price_include'}:
            self._remediation_error(_('Fiscal product remediation sales tax fields are invalid.'))
        if not isinstance(products_payload, list) or not 1 <= len(products_payload) <= 500:
            self._remediation_error(_('Fiscal product remediation requires 1 to 500 products.'))

        product_rows = {}
        for row in products_payload:
            if not isinstance(row, dict) or set(row) != {
                    'odoo_product_id', 'remediate_income_account', 'remediate_customer_taxes'}:
                self._remediation_error(_('Fiscal product remediation product fields are invalid.'))
            product_id = row['odoo_product_id']
            account_flag = row['remediate_income_account']
            tax_flag = row['remediate_customer_taxes']
            if (
                not self._positive_int(product_id)
                or not isinstance(account_flag, bool) or not isinstance(tax_flag, bool)
                or not (account_flag or tax_flag) or product_id in product_rows
            ):
                self._remediation_error(_('Fiscal product remediation product entry is invalid.'))
            product_rows[product_id] = row

        company = self._fiscal_company()
        fiscal_country = company.account_fiscal_country_id
        if (
            not self._positive_int(company_payload['odoo_id'])
            or company_payload['odoo_id'] != company.id
            or company_payload['country_code'] != (fiscal_country.code if fiscal_country else None)
            or company_payload['country_code'] != 'ES'
            or company_payload['currency'] != company.currency_id.name
        ):
            self._remediation_error(_('Fiscal product remediation company does not match Odoo.'))
        expected = contract.get('expected') if isinstance(contract, dict) else None
        if not isinstance(contract, dict) or contract.get('contract_version') != 1 or not isinstance(expected, dict):
            self._remediation_error(_('Current platform fiscal contract is invalid.'))
        if expected.get('currency') != company.currency_id.name:
            self._remediation_error(_('Current platform fiscal currency does not match Odoo.'))
        customer_id = customer_payload['odoo_id']
        if (
            not self._positive_int(customer_id)
            or customer_id != expected.get('customer_odoo_id')
            or not self.env['res.partner'].sudo().browse(customer_id).exists()
        ):
            self._remediation_error(_('Fiscal product remediation customer is not the fiscal customer.'))

        account_id = account_payload['odoo_id']
        if not self._positive_int(account_id) or account_payload['account_type'] not in ('income', 'income_other'):
            self._remediation_error(_('Fiscal product remediation income account is invalid.'))
        account = self.env['account.account'].sudo().with_company(company).browse(account_id).exists()
        if (
            not account
            or account.code != account_payload['code']
            or account.account_type != account_payload['account_type']
            or account.code != str(expected.get('income_account_code'))
            or not self._account_applies_to_company(account, company)
        ):
            self._remediation_error(_('Fiscal product remediation income account does not match Odoo.'))

        tax_id = tax_payload['odoo_tax_id']
        rate = tax_payload['rate']
        if not self._positive_int(tax_id) or not self._number(rate) or rate <= 0:
            self._remediation_error(_('Fiscal product remediation sales tax is invalid.'))
        tax = self.env['account.tax'].sudo().with_context(active_test=False).browse(tax_id).exists()
        try:
            expected_rate = float(expected.get('vat_rate'))
        except (TypeError, ValueError):
            self._remediation_error(_('Current platform fiscal VAT rate is invalid.'))
        if (
            not tax or not tax.active or tax.company_id != company
            or tax.type_tax_use != 'sale' or tax_payload['type_tax_use'] != 'sale'
            or tax.amount_type != 'percent' or tax_payload['amount_type'] != 'percent'
            or tax.amount != rate or expected_rate != rate
            or not tax.country_id or tax.country_id.code != 'ES'
            or tax.country_id != fiscal_country
            or tax_payload['country_code'] != 'ES'
            or not isinstance(tax_payload['price_include'], bool)
            or bool(tax.price_include) != tax_payload['price_include']
        ):
            self._remediation_error(_('Fiscal product remediation sales tax does not match Odoo.'))

        products = self.env['product.product'].sudo().with_context(active_test=False).browse(
            list(product_rows)
        ).exists()
        if len(products) != len(product_rows) or any(
                not product.active or not product.softlife_recipe_id for product in products):
            self._remediation_error(_('Fiscal product remediation contains an inactive or non-SoftLife product.'))
        supplied_ids = set(product_rows)
        for template in products.product_tmpl_id:
            variant_ids = set(template.with_context(active_test=False).product_variant_ids.ids)
            if len(variant_ids) > 1 and not variant_ids.issubset(supplied_ids):
                self._remediation_error(_(
                    'Fiscal product remediation must include every variant of a product template.'
                ))
            variant_rows = [product_rows[variant_id] for variant_id in variant_ids]
            if len(variant_ids) > 1 and (
                len({row['remediate_income_account'] for row in variant_rows}) != 1
                or len({row['remediate_customer_taxes'] for row in variant_rows}) != 1
            ):
                self._remediation_error(_(
                    'Fiscal product remediation flags must match for every variant of a template.'
                ))
        return company, self.env['res.partner'].sudo().browse(customer_id), account, tax, products, product_rows

    @staticmethod
    def _positive_int(value):
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _nonnegative_int(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    @api.model
    def remediate_fiscal_products(self, payload):
        if not self._fiscal_product_remediation_enabled():
            raise SoftlifeAPIError(
                _('Fiscal product remediation is disabled in Odoo Settings.'),
                code='fiscal_product_remediation_disabled',
            )
        contract = self._api_request('GET', '/api/internal/odoo/fiscal-configuration')
        company, customer, account, tax, products, rows = \
            self._validate_fiscal_product_remediation(payload, contract)
        fiscal_position = self.env['account.fiscal.position'].sudo().with_company(
            company,
        )._get_fiscal_position(customer.with_company(company))
        all_company_ids = self.env['res.company'].sudo().search([]).ids
        with self.env.cr.savepoint():
            for template in products.product_tmpl_id:
                template_products = products.filtered(lambda product: product.product_tmpl_id == template)
                if any(rows[product.id]['remediate_income_account'] for product in template_products):
                    template.sudo().with_company(company).property_account_income_id = account
                if any(rows[product.id]['remediate_customer_taxes'] for product in template_products):
                    template_all_companies = template.sudo().with_context(
                        active_test=False, allowed_company_ids=all_company_ids,
                    )
                    other_tax_ids = template_all_companies.taxes_id.filtered(
                        lambda current_tax: current_tax.company_id != company
                    ).ids
                    template_all_companies.with_company(company).taxes_id = [(6, 0, other_tax_ids + tax.ids)]
            for product in products:
                effective_account, effective_taxes = self._effective_product_fiscal_configuration(
                    product.sudo(), company, fiscal_position,
                )
                row = rows[product.id]
                if row['remediate_income_account'] and effective_account != account:
                    self._remediation_error(_(
                        'Fiscal product remediation income account verification failed.'
                    ))
                if row['remediate_customer_taxes'] and effective_taxes != tax:
                    self._remediation_error(_(
                        'Fiscal product remediation sales tax verification failed.'
                    ))
            verification_payload = self._fiscal_configuration_payload(contract)
            verification = self._api_request(
                'POST', '/api/internal/odoo/fiscal-configuration', payload=verification_payload,
            )
        result = {
            'accepted': True,
            'summary': _('Remediated and verified %s fiscal product configuration(s).') % len(products),
        }
        report_id = (
            verification.get('configuration_report_id')
            or verification.get('report_id')
            or verification.get('id')
        )
        report_hash = verification.get('configuration_payload_sha256') or verification.get('payload_sha256')
        if report_id:
            result['verification_report_id'] = report_id
        if report_hash:
            result['verification_payload_sha256'] = report_hash
        return result

    @api.model
    def _fiscal_invoice_error(self, message):
        raise SoftlifeAPIError(message, code='invalid_fiscal_invoice_request')

    @staticmethod
    def _canonical_sha256(payload):
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode()).hexdigest()

    @api.model
    def _current_fiscal_invoice_configuration(self, contract):
        expected = contract.get('expected') if isinstance(contract, dict) else None
        required = ('journal_code', 'customer_odoo_id', 'vat_rate', 'currency', 'income_account_code')
        if (
            not isinstance(contract, dict) or contract.get('contract_version') != 1
            or isinstance(contract.get('contract_version'), bool)
            or not isinstance(expected, dict)
            or any(expected.get(field) in (None, '') for field in required)
            or expected.get('tax_treatment_approved') is not True
        ):
            self._fiscal_invoice_error(_('Current platform fiscal contract is invalid.'))
        company = self._fiscal_company()
        fiscal_country = company.account_fiscal_country_id
        if (
            not fiscal_country or fiscal_country.code != 'ES'
            or company.currency_id.name != 'EUR' or expected['currency'] != 'EUR'
        ):
            self._fiscal_invoice_error(_('Current fiscal company must be Spanish and use EUR.'))
        customer_id = expected['customer_odoo_id']
        if not self._positive_int(customer_id):
            self._fiscal_invoice_error(_('Current platform fiscal customer is invalid.'))
        customer = self.env['res.partner'].sudo().with_context(active_test=False).browse(
            customer_id,
        ).exists()
        if not customer or not customer.active or (customer.company_id and customer.company_id != company):
            self._fiscal_invoice_error(_('Current platform fiscal customer is unavailable.'))
        journal = self.env['account.journal'].sudo().with_company(company).with_context(
            active_test=False,
        ).search([
            ('company_id', '=', company.id), ('code', '=', str(expected['journal_code'])),
        ], limit=1)
        if not journal or not journal.active or journal.type != 'sale':
            self._fiscal_invoice_error(_('Current platform fiscal sales journal is invalid.'))
        try:
            rate = float(expected['vat_rate'])
        except (TypeError, ValueError):
            self._fiscal_invoice_error(_('Current platform fiscal VAT rate is invalid.'))
        if not math.isfinite(rate) or rate <= 0:
            self._fiscal_invoice_error(_('Current platform fiscal VAT rate is invalid.'))
        return company, journal, customer, rate

    @api.model
    def _validate_fiscal_invoice_tax(self, tax_payload, company, expected_rate):
        if not isinstance(tax_payload, dict) or set(tax_payload) != {
                'odoo_tax_id', 'rate', 'country_code', 'type_tax_use', 'amount_type',
                'price_include'}:
            self._fiscal_invoice_error(_('Fiscal invoice tax fields are invalid.'))
        tax_id = tax_payload['odoo_tax_id']
        rate = tax_payload['rate']
        if (
            not self._positive_int(tax_id) or not self._number(rate) or rate <= 0
            or tax_payload['country_code'] != 'ES' or tax_payload['type_tax_use'] != 'sale'
            or tax_payload['amount_type'] != 'percent'
            or not isinstance(tax_payload['price_include'], bool)
        ):
            self._fiscal_invoice_error(_('Fiscal invoice tax is invalid.'))
        tax = self.env['account.tax'].sudo().with_context(active_test=False).browse(tax_id).exists()
        if (
            not tax or not tax.active or tax.company_id != company
            or tax.country_id != company.account_fiscal_country_id
            or tax.type_tax_use != 'sale' or tax.amount_type != 'percent'
            or tax.amount != rate or rate != expected_rate
            or bool(tax.price_include) != tax_payload['price_include']
        ):
            self._fiscal_invoice_error(_('Fiscal invoice tax does not match Odoo.'))
        return tax

    @api.model
    def _validate_fiscal_invoice_move(self, move, invoice, configuration, tax):
        company, journal, customer, _rate = configuration
        self._validate_fiscal_invoice_snapshot(move, invoice)
        if (
            move.softlife_fiscal_payload_sha256 != invoice['invoice_payload_sha256']
            or move.company_id != company or move.journal_id != journal
            or move.partner_id != customer or move.move_type != 'out_invoice'
            or move.currency_id != company.currency_id
            or fields.Date.to_string(move.invoice_date) != invoice['invoice_date']
            or fields.Date.to_string(move.date) != invoice['invoice_date']
            or move.ref != invoice['reference'] or move.state not in ('draft', 'posted')
        ):
            self._fiscal_invoice_error(_('Existing fiscal invoice does not match the frozen payload.'))
        lines = move.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
        if len(lines) != len(invoice['lines']):
            self._fiscal_invoice_error(_('Existing fiscal invoice lines do not match the frozen payload.'))
        for line, expected in zip(lines, invoice['lines']):
            price_unit = self._fiscal_invoice_price_unit(expected, tax.price_include)
            if (
                line.product_id.id != expected['odoo_product_id']
                or line.name != expected['description'] or line.quantity != expected['quantity']
                or abs(line.price_unit - price_unit) > 1e-9
                or line.account_id.id != expected['account_id'] or line.tax_ids != tax
                or self._amount_cents(line.price_total) != expected['gross_cents']
                or self._amount_cents(line.price_subtotal) != expected['tax_base_cents']
            ):
                self._fiscal_invoice_error(_('Existing fiscal invoice lines do not match the frozen payload.'))
        if self._amount_cents(move.amount_total) != invoice['expected_total_cents']:
            self._fiscal_invoice_error(_('Existing fiscal invoice total does not match the frozen payload.'))

    @api.model
    def _fiscal_invoice_snapshot_sha256(self, invoice):
        try:
            frozen = copy.deepcopy(invoice)
            invoice_hash = frozen.pop('invoice_payload_sha256')
            for line in frozen['lines']:
                account_id = line.pop('account_id')
                if not self._positive_int(account_id):
                    self._fiscal_invoice_error(_('Stored fiscal invoice snapshot account is invalid.'))
        except (AttributeError, KeyError, TypeError):
            self._fiscal_invoice_error(_('Stored fiscal invoice snapshot is invalid.'))
        computed = self._canonical_sha256(frozen)
        if not isinstance(invoice_hash, str) or computed != invoice_hash:
            self._fiscal_invoice_error(_('Stored fiscal invoice snapshot hash is invalid.'))
        return computed

    @api.model
    def _validate_fiscal_invoice_snapshot(self, move, invoice=None):
        snapshot = move.softlife_fiscal_invoice_snapshot
        if not isinstance(snapshot, dict):
            self._fiscal_invoice_error(_('Stored fiscal invoice snapshot is missing.'))
        self._fiscal_invoice_snapshot_sha256(snapshot)
        if (
            snapshot.get('platform_invoice_id') != move.softlife_fiscal_invoice_id
            or snapshot.get('invoice_payload_sha256') != move.softlife_fiscal_payload_sha256
            or (invoice is not None and snapshot != invoice)
        ):
            self._fiscal_invoice_error(_('Stored fiscal invoice snapshot does not match provenance.'))
        return copy.deepcopy(snapshot)

    @staticmethod
    def _amount_cents(amount):
        return int((Decimal(str(amount)) * 100).quantize(Decimal('1')))

    @staticmethod
    def _fiscal_invoice_price_unit(line, price_include):
        cents = line['gross_cents'] if price_include else line['tax_base_cents']
        return float(Decimal(cents) / Decimal(100) / Decimal(line['quantity']))

    @api.model
    def _validate_fiscal_invoice_draft_payload(self, payload, contract):
        if not isinstance(payload, dict) or set(payload) != {
                'contract_version', 'configuration_report_id', 'configuration_payload_sha256',
                'company', 'journal', 'customer', 'tax', 'invoices'}:
            self._fiscal_invoice_error(_('Fiscal invoice draft payload fields are invalid.'))
        if payload['contract_version'] != 1 or isinstance(payload['contract_version'], bool):
            self._fiscal_invoice_error(_('Fiscal invoice draft contract version is unsupported.'))
        if (
            not isinstance(payload['configuration_report_id'], str)
            or not payload['configuration_report_id'].strip()
            or not isinstance(payload['configuration_payload_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', payload['configuration_payload_sha256'])
        ):
            self._fiscal_invoice_error(_('Fiscal invoice configuration report identity is invalid.'))
        if not isinstance(payload['company'], dict) or set(payload['company']) != {
                'odoo_id', 'country_code', 'currency'}:
            self._fiscal_invoice_error(_('Fiscal invoice company fields are invalid.'))
        if not isinstance(payload['journal'], dict) or set(payload['journal']) != {'code'}:
            self._fiscal_invoice_error(_('Fiscal invoice journal fields are invalid.'))
        if not isinstance(payload['customer'], dict) or set(payload['customer']) != {'odoo_id'}:
            self._fiscal_invoice_error(_('Fiscal invoice customer fields are invalid.'))
        invoices = payload['invoices']
        if not isinstance(invoices, list) or not 1 <= len(invoices) <= 500:
            self._fiscal_invoice_error(_('Fiscal invoice draft requires 1 to 500 invoices.'))

        # Keep validation annotations out of the immutable queue payload supplied by the caller.
        payload = copy.deepcopy(payload)
        invoices = payload['invoices']

        configuration = self._current_fiscal_invoice_configuration(contract)
        company, journal, customer, expected_rate = configuration
        if not self._positive_int(payload['company']['odoo_id']) or payload['company'] != {
                'odoo_id': company.id, 'country_code': 'ES', 'currency': 'EUR'}:
            self._fiscal_invoice_error(_('Fiscal invoice company does not match Odoo.'))
        if (
            not isinstance(payload['journal']['code'], str)
            or not payload['journal']['code'].strip()
            or payload['journal']['code'] != journal.code
        ):
            self._fiscal_invoice_error(_('Fiscal invoice journal does not match the current contract.'))
        if (
            not self._positive_int(payload['customer']['odoo_id'])
            or payload['customer']['odoo_id'] != customer.id
        ):
            self._fiscal_invoice_error(_('Fiscal invoice customer is not the current final consumer.'))
        tax = self._validate_fiscal_invoice_tax(payload['tax'], company, expected_rate)

        invoice_ids = set()
        product_ids = set()
        for invoice in invoices:
            invoice_fields = {
                'platform_invoice_id', 'invoice_payload_sha256', 'move_type', 'invoice_date',
                'currency', 'reference', 'expected_total_cents', 'lines',
            }
            if not isinstance(invoice, dict) or set(invoice) not in (
                    invoice_fields, invoice_fields | {'zero_value_reason'}):
                self._fiscal_invoice_error(_('Fiscal invoice fields are invalid.'))
            platform_id = invoice['platform_invoice_id']
            try:
                valid_uuid = isinstance(platform_id, str) and str(uuid.UUID(platform_id)) == platform_id
            except (ValueError, AttributeError):
                valid_uuid = False
            invoice_hash = invoice['invoice_payload_sha256']
            frozen_invoice = dict(invoice)
            frozen_invoice.pop('invoice_payload_sha256', None)
            if (
                not valid_uuid or platform_id in invoice_ids
                or not isinstance(invoice_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', invoice_hash)
                or self._canonical_sha256(frozen_invoice) != invoice_hash
                or invoice['move_type'] != 'out_invoice' or invoice['currency'] != 'EUR'
                or not isinstance(invoice['reference'], str) or not invoice['reference'].strip()
                or not self._nonnegative_int(invoice['expected_total_cents'])
                or not isinstance(invoice['invoice_date'], str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', invoice['invoice_date'])
            ):
                self._fiscal_invoice_error(_('Fiscal invoice identity or header is invalid.'))
            try:
                if datetime.date.fromisoformat(invoice['invoice_date']).isoformat() != invoice['invoice_date']:
                    raise ValueError
            except ValueError:
                self._fiscal_invoice_error(_('Fiscal invoice date is invalid.'))
            lines = invoice['lines']
            zero_value_reason = invoice.get('zero_value_reason')
            if (
                zero_value_reason not in (None, 'free', 'admin_override')
                or (invoice['expected_total_cents'] == 0) != (zero_value_reason is not None)
            ):
                self._fiscal_invoice_error(_('Fiscal invoice zero-value reason is invalid.'))
            if not isinstance(lines, list) or not 1 <= len(lines) <= 100:
                self._fiscal_invoice_error(_('Fiscal invoice requires 1 to 100 lines.'))
            gross_total = 0
            for line in lines:
                if not isinstance(line, dict) or set(line) != {
                        'odoo_product_id', 'description', 'quantity', 'gross_cents',
                        'tax_base_cents', 'vat_cents', 'odoo_tax_id'}:
                    self._fiscal_invoice_error(_('Fiscal invoice line fields are invalid.'))
                if (
                    not self._positive_int(line['odoo_product_id'])
                    or not isinstance(line['description'], str) or not line['description'].strip()
                    or not self._positive_int(line['quantity'])
                    or not self._nonnegative_int(line['gross_cents'])
                    or not isinstance(line['tax_base_cents'], int)
                    or isinstance(line['tax_base_cents'], bool) or line['tax_base_cents'] < 0
                    or not isinstance(line['vat_cents'], int) or isinstance(line['vat_cents'], bool)
                    or line['vat_cents'] < 0 or not self._positive_int(line['odoo_tax_id'])
                    or line['odoo_tax_id'] != tax.id
                    or line['gross_cents'] != line['tax_base_cents'] + line['vat_cents']
                ):
                    self._fiscal_invoice_error(_('Fiscal invoice line values are invalid.'))
                rate = Decimal(str(tax.amount)) / Decimal(100)
                if tax.price_include:
                    computed_base = (Decimal(line['gross_cents']) / (Decimal(1) + rate)).quantize(
                        Decimal('1'), rounding=ROUND_HALF_UP,
                    )
                    computed_vat = Decimal(line['gross_cents']) - computed_base
                else:
                    computed_vat = (Decimal(line['tax_base_cents']) * rate).quantize(
                        Decimal('1'), rounding=ROUND_HALF_UP,
                    )
                if computed_vat != line['vat_cents']:
                    self._fiscal_invoice_error(_('Fiscal invoice line VAT cents are invalid.'))
                gross_total += line['gross_cents']
                product_ids.add(line['odoo_product_id'])
            if gross_total != invoice['expected_total_cents']:
                self._fiscal_invoice_error(_('Fiscal invoice line cents do not match its expected total.'))
            invoice_ids.add(platform_id)

        products = self.env['product.product'].sudo().with_company(company).with_context(
            active_test=False,
        ).browse(list(product_ids)).exists()
        products_by_id = {product.id: product for product in products}
        if len(products_by_id) != len(product_ids):
            self._fiscal_invoice_error(_('Fiscal invoice contains an unknown product.'))
        fiscal_position = self.env['account.fiscal.position'].sudo().with_company(
            company,
        )._get_fiscal_position(customer.with_company(company))
        for invoice in invoices:
            for line in invoice['lines']:
                product = products_by_id[line['odoo_product_id']]
                account, taxes = self._effective_product_fiscal_configuration(
                    product, company, fiscal_position,
                )
                if (
                    not product.active or not product.sale_ok or not product.softlife_recipe_id
                    or not account or account.account_type not in ('income', 'income_other')
                    or account.code != str(contract['expected']['income_account_code'])
                    or not self._account_applies_to_company(account, company) or taxes != tax
                ):
                    self._fiscal_invoice_error(_(
                        'Fiscal invoice product is inactive, unsaleable, unlinked, or fiscally misconfigured.'
                    ))
                line['account_id'] = account.id

        existing = self.env['account.move'].sudo().with_context(active_test=False).search([
            ('softlife_fiscal_invoice_id', 'in', list(invoice_ids)),
        ])
        existing_by_id = {move.softlife_fiscal_invoice_id: move for move in existing}
        for invoice in invoices:
            move = existing_by_id.get(invoice['platform_invoice_id'])
            if move:
                self._validate_fiscal_invoice_move(move, invoice, configuration, tax)
                if move.state != 'draft':
                    self._fiscal_invoice_error(_(
                        'Fiscal invoice draft creation cannot reuse a posted invoice.'
                    ))
        return configuration, tax, existing_by_id, payload

    @api.model
    def _create_fiscal_invoice_drafts(self, payload):
        if not self._fiscal_invoice_draft_creation_enabled():
            raise SoftlifeAPIError(
                _('Fiscal invoice draft creation is disabled in Odoo Settings.'),
                code='fiscal_invoice_draft_creation_disabled',
            )
        contract = self._api_request('GET', '/api/internal/odoo/fiscal-configuration')
        configuration, tax, existing_by_id, payload = self._validate_fiscal_invoice_draft_payload(
            payload, contract,
        )
        company, journal, customer, _rate = configuration
        results = []
        with self.env.cr.savepoint():
            for invoice in payload['invoices']:
                move = existing_by_id.get(invoice['platform_invoice_id'])
                created = not move
                if not move:
                    line_commands = []
                    for line in invoice['lines']:
                        line_commands.append((0, 0, {
                            'product_id': line['odoo_product_id'],
                            'name': line['description'],
                            'quantity': line['quantity'],
                            'price_unit': self._fiscal_invoice_price_unit(line, tax.price_include),
                            'account_id': line['account_id'],
                            'tax_ids': [(6, 0, tax.ids)],
                        }))
                    with _softlife_fiscal_internal_scope():
                        move = self.env['account.move'].sudo().with_company(company).create({
                            'company_id': company.id,
                            'journal_id': journal.id,
                            'partner_id': customer.id,
                            'move_type': 'out_invoice',
                            'currency_id': company.currency_id.id,
                            'date': invoice['invoice_date'],
                            'invoice_date': invoice['invoice_date'],
                            'ref': invoice['reference'],
                            'invoice_line_ids': line_commands,
                            'softlife_fiscal_invoice_id': invoice['platform_invoice_id'],
                            'softlife_fiscal_payload_sha256': invoice['invoice_payload_sha256'],
                            'softlife_fiscal_invoice_snapshot': invoice,
                        })
                    self._validate_fiscal_invoice_move(move, invoice, configuration, tax)
                results.append({
                    'platform_invoice_id': invoice['platform_invoice_id'],
                    'odoo_move_id': move.id,
                    'invoice_payload_sha256': invoice['invoice_payload_sha256'],
                    'state': move.state,
                    'created': created,
                })
        return {
            'accepted': True,
            'summary': _('Created or verified %s fiscal invoice draft(s).') % len(results),
            'contract_version': 1,
            'invoices': results,
        }

    @api.model
    def _confirm_fiscal_invoices(self, payload):
        if not self._fiscal_invoice_confirmation_enabled():
            raise SoftlifeAPIError(
                _('Fiscal invoice confirmation is disabled in Odoo Settings.'),
                code='fiscal_invoice_confirmation_disabled',
            )
        if not isinstance(payload, dict) or set(payload) != {'contract_version', 'company', 'invoices'}:
            self._fiscal_invoice_error(_('Fiscal invoice confirmation payload fields are invalid.'))
        if payload['contract_version'] != 1 or isinstance(payload['contract_version'], bool):
            self._fiscal_invoice_error(_('Fiscal invoice confirmation contract version is unsupported.'))
        if not isinstance(payload['company'], dict) or set(payload['company']) != {'odoo_id'}:
            self._fiscal_invoice_error(_('Fiscal invoice confirmation company fields are invalid.'))
        invoices = payload['invoices']
        if not isinstance(invoices, list) or not 1 <= len(invoices) <= 500:
            self._fiscal_invoice_error(_('Fiscal invoice confirmation requires 1 to 500 invoices.'))
        contract = self._api_request('GET', '/api/internal/odoo/fiscal-configuration')
        configuration = self._current_fiscal_invoice_configuration(contract)
        company, journal, customer, _rate = configuration
        if (
            not self._positive_int(payload['company']['odoo_id'])
            or payload['company']['odoo_id'] != company.id
        ):
            self._fiscal_invoice_error(_('Fiscal invoice confirmation company does not match Odoo.'))
        platform_ids = set()
        move_ids = set()
        for row in invoices:
            if not isinstance(row, dict) or set(row) != {
                    'platform_invoice_id', 'odoo_move_id', 'invoice_payload_sha256'}:
                self._fiscal_invoice_error(_('Fiscal invoice confirmation fields are invalid.'))
            try:
                valid_uuid = (
                    isinstance(row['platform_invoice_id'], str)
                    and str(uuid.UUID(row['platform_invoice_id'])) == row['platform_invoice_id']
                )
            except (ValueError, AttributeError):
                valid_uuid = False
            if (
                not valid_uuid or row['platform_invoice_id'] in platform_ids
                or not self._positive_int(row['odoo_move_id']) or row['odoo_move_id'] in move_ids
                or not isinstance(row['invoice_payload_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['invoice_payload_sha256'])
            ):
                self._fiscal_invoice_error(_('Fiscal invoice confirmation identity is invalid.'))
            platform_ids.add(row['platform_invoice_id'])
            move_ids.add(row['odoo_move_id'])
        self.env.cr.execute(
            'SELECT id FROM account_move WHERE id IN %s FOR UPDATE',
            [tuple(move_ids)],
        )
        locked_ids = {row[0] for row in self.env.cr.fetchall()}
        if locked_ids != move_ids:
            self._fiscal_invoice_error(_('Fiscal invoice confirmation contains an unknown move.'))
        moves = self.env['account.move'].sudo().browse(list(move_ids))
        moves.invalidate_recordset()
        moves_by_id = {move.id: move for move in moves}
        ordered = []
        snapshots = {}
        for row in invoices:
            move = moves_by_id[row['odoo_move_id']]
            if (
                move.softlife_fiscal_invoice_id != row['platform_invoice_id']
                or move.softlife_fiscal_payload_sha256 != row['invoice_payload_sha256']
                or move.company_id != company or move.journal_id != journal
                or move.partner_id != customer or move.move_type != 'out_invoice'
                or move.currency_id != company.currency_id or move.state not in ('draft', 'posted')
            ):
                self._fiscal_invoice_error(_('Fiscal invoice confirmation move does not match Odoo.'))
            snapshot = self._validate_fiscal_invoice_snapshot(move)
            tax_ids = {line.get('odoo_tax_id') for line in snapshot.get('lines', [])}
            if len(tax_ids) != 1:
                self._fiscal_invoice_error(_('Stored fiscal invoice snapshot tax is invalid.'))
            tax = self.env['account.tax'].sudo().with_context(active_test=False).browse(
                tax_ids.pop(),
            ).exists()
            if not tax:
                self._fiscal_invoice_error(_('Stored fiscal invoice snapshot tax is unavailable.'))
            tax = self._validate_fiscal_invoice_tax({
                'odoo_tax_id': tax.id,
                'rate': tax.amount,
                'country_code': tax.country_id.code if tax.country_id else None,
                'type_tax_use': tax.type_tax_use,
                'amount_type': tax.amount_type,
                'price_include': bool(tax.price_include),
            }, company, configuration[3])
            self._validate_fiscal_invoice_move(move, snapshot, configuration, tax)
            snapshots[move.id] = (snapshot, tax)
            ordered.append(move)
        was_draft = {move.id: move.state == 'draft' for move in ordered}
        with self.env.cr.savepoint():
            drafts = self.env['account.move'].browse([
                move.id for move in ordered if was_draft[move.id]
            ])
            if drafts:
                with _softlife_fiscal_internal_scope():
                    drafts.action_post()
            for move in ordered:
                snapshot, tax = snapshots[move.id]
                self._validate_fiscal_invoice_move(move, snapshot, configuration, tax)
                if move.state != 'posted' or not move.name:
                    self._fiscal_invoice_error(_(
                        'Fiscal invoice confirmation did not post every listed move.'
                    ))
        return {
            'accepted': True,
            'summary': _('Confirmed or verified %s fiscal invoice(s).') % len(ordered),
            'contract_version': 1,
            'invoices': [{
                'platform_invoice_id': row['platform_invoice_id'],
                'odoo_move_id': move.id,
                'invoice_payload_sha256': row['invoice_payload_sha256'],
                'state': 'posted',
                'name': move.name,
                'confirmed': was_draft[move.id],
            } for row, move in zip(invoices, ordered)],
        }

    @api.model
    def _rest_get(self, table, params=None):
        import requests
        base = self._param('softlife.sync.supabase_url').rstrip('/')
        key = self._param('softlife.sync.supabase_key')
        url = f'{base}/rest/v1/{table}'
        headers = {'apikey': key, 'Authorization': f'Bearer {key}'}
        r = requests.get(url, headers=headers, params=params or {}, timeout=60)
        if r.status_code != 200:
            raise UserError(_('Supabase GET %s failed: %s %s') % (table, r.status_code, r.text[:200]))
        return r.json()

    @api.model
    def _rest_upsert(self, table, rows, on_conflict):
        """Bulk upsert rows into a Supabase table, keyed on `on_conflict` (a column name)."""
        import requests
        if not rows:
            return
        base = self._param('softlife.sync.supabase_url').rstrip('/')
        key = self._param('softlife.sync.supabase_key')
        url = f'{base}/rest/v1/{table}'
        headers = {
            'apikey': key,
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'Prefer': 'resolution=merge-duplicates,return=minimal',
        }
        r = requests.post(url, headers=headers, params={'on_conflict': on_conflict}, json=rows, timeout=60)
        if r.status_code not in (200, 201, 204):
            raise UserError(_('Supabase upsert %s failed: %s %s') % (table, r.status_code, r.text[:200]))

    @api.model
    def _rest_delete_missing(self, table, id_column, present_ids):
        """Delete rows from a Supabase table whose id_column isn't in present_ids —
        i.e. records removed/archived on the Odoo side since the last sync.
        No-ops on an empty present_ids so a transient empty read can't wipe the table."""
        import requests
        if not present_ids:
            return
        base = self._param('softlife.sync.supabase_url').rstrip('/')
        key = self._param('softlife.sync.supabase_key')
        url = f'{base}/rest/v1/{table}'
        headers = {'apikey': key, 'Authorization': f'Bearer {key}', 'Prefer': 'return=minimal'}
        ids_csv = ','.join(str(i) for i in present_ids)
        r = requests.delete(url, headers=headers, params={id_column: f'not.in.({ids_csv})'}, timeout=60)
        if r.status_code not in (200, 204):
            raise UserError(_('Supabase delete-missing %s failed: %s %s') % (table, r.status_code, r.text[:200]))

    @api.model
    def _rest_rpc(self, function, payload):
        import requests
        base = self._param('softlife.sync.supabase_url').rstrip('/')
        key = self._param('softlife.sync.supabase_key')
        r = requests.post(
            f'{base}/rest/v1/rpc/{function}',
            headers={'apikey': key, 'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json=payload, timeout=60,
        )
        if r.status_code not in (200, 201, 204):
            raise UserError(_('Supabase RPC %s failed: %s %s') % (function, r.status_code, r.text[:200]))

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------
    @api.model
    def _acquire_sync_lock(self):
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s)', [self._sync_lock_id])
        return bool(self.env.cr.fetchone()[0])

    @api.model
    def sync_partners(self):
        rows = self._rest_get('tenants', {'select': 'id,name,kind'})
        Partner = self.env['res.partner']
        n = 0
        for row in rows:
            sid = row.get('id')
            if not sid:
                continue
            vals = {'name': row.get('name') or 'SoftLife tenant', 'supabase_id': sid, 'is_company': True}
            existing = Partner.search([('supabase_id', '=', sid)], limit=1)
            if existing:
                existing.write(vals)
            else:
                Partner.create(vals)
            n += 1
        return n

    @api.model
    def sync_products(self):
        """Platform -> Odoo. Creates/updates product.template by supabase_id.
        Does NOT touch products.odoo_id — linking a platform ingredient to an
        Odoo SKU is a deliberate choice made on the platform (see /odoo and
        /products), never inferred automatically. An earlier version of this
        method auto-linked by writing back the newly-created product's id,
        which silently created duplicate Odoo products for ingredients that
        already had a real match under a different id (matched by name only
        in the human's head, not by any field this code could see) and linked
        to the wrong one. Don't repeat that."""
        rows = self._rest_get('products', {'select': 'id,name,type'})
        Template = self.env['product.template']
        n = 0
        for row in rows:
            sid = row.get('id')
            if not sid:
                continue
            vals = {'name': row.get('name') or 'SoftLife product', 'supabase_id': sid, 'type': 'consu'}
            existing = Template.search([('supabase_id', '=', sid)], limit=1)
            if existing:
                existing.write(vals)
            else:
                Template.create(vals)
            n += 1
        return n

    # ------------------------------------------------------------------
    # Odoo -> Supabase (master-data mirror the platform reads)
    # ------------------------------------------------------------------
    @api.model
    def sync_odoo_warehouses(self):
        Warehouse = self.env['stock.warehouse']
        rows = [
            {'odoo_id': w.id, 'name': w.name, 'code': w.code,
             'stock_location_id': w.lot_stock_id.id if w.lot_stock_id else None}
            for w in Warehouse.search([])
        ]
        self._rest_upsert('odoo_warehouses', rows, on_conflict='odoo_id')
        self._rest_delete_missing('odoo_warehouses', 'odoo_id', [r['odoo_id'] for r in rows])
        return len(rows)

    @api.model
    def sync_odoo_products(self):
        Product = self.env['product.product']
        rows = []
        for p in Product.search([('active', '=', True)]):
            rows.append({
                'odoo_id': p.id,
                'name': p.display_name,
                'sku': p.default_code or None,
                'barcode': p.barcode or None,
                'category': p.categ_id.display_name if p.categ_id else None,
                'uom': p.uom_id.name if p.uom_id else None,
                'uom_rounding': p.uom_id.rounding if p.uom_id else 0.01,
                'tracking': p.tracking,
                'qty_available': p.qty_available,
                'package_content_quantity': p.package_content_quantity or None,
                'package_content_uom': p.package_content_uom or None,
            })
        self._rest_upsert('odoo_products', rows, on_conflict='odoo_id')
        # Deleted/archived in Odoo -> drop from the mirror. Any platform ingredient
        # linked to it (products.odoo_id) is auto-unlinked (FK is ON DELETE SET NULL),
        # never silently re-pointed at something else.
        self._rest_delete_missing('odoo_products', 'odoo_id', [r['odoo_id'] for r in rows])
        return len(rows)

    @api.model
    def sync_odoo_lots(self):
        # lot.location_id / product_qty are Odoo's own computed snapshot of where
        # a lot currently sits and how much remains (aggregated across quants).
        # A lot split across multiple locations collapses to one row here —
        # fine for a "what lots exist and roughly where" mirror, not for
        # location-level stock accounting.
        Lot = self.env['stock.lot']
        rows = []
        for lot in Lot.search([]):
            warehouse = lot.location_id.warehouse_id if lot.location_id else None
            rows.append({
                'odoo_id': lot.id,
                'name': lot.name,
                'odoo_product_id': lot.product_id.id or None,
                'product_name': lot.product_id.display_name if lot.product_id else None,
                'qty': lot.product_qty,
                'expiration_date': lot.expiration_date.date().isoformat() if lot.expiration_date else None,
                'odoo_warehouse_id': warehouse.id if warehouse else None,
                'warehouse_name': warehouse.name if warehouse else None,
            })
        self._rest_upsert('odoo_lots', rows, on_conflict='odoo_id')
        self._rest_delete_missing('odoo_lots', 'odoo_id', [r['odoo_id'] for r in rows])
        return len(rows)

    @api.model
    def sync_odoo_lot_stock(self):
        lot_quantities = {}
        product_quantities = {}
        for quant in self.env['stock.quant'].search([
            ('location_id.usage', '=', 'internal'),
            ('product_id.active', '=', True),
        ]):
            warehouse = quant.location_id.warehouse_id
            if not warehouse:
                continue
            product_key = (quant.product_id.id, warehouse.id)
            current = product_quantities.get(product_key, [0.0, 0.0, 0.0])
            current[0] += quant.quantity
            current[1] += quant.reserved_quantity
            current[2] = max(0.0, current[0] - current[1])
            product_quantities[product_key] = current
            if quant.lot_id:
                lot_key = (quant.lot_id.id, warehouse.id)
                lot_current = lot_quantities.get(lot_key, [0.0, 0.0])
                lot_current[0] += quant.quantity
                lot_current[1] += quant.reserved_quantity
                lot_quantities[lot_key] = lot_current
        rows = [
            {
                'odoo_lot_id': lot_id,
                'odoo_warehouse_id': warehouse_id,
                'qty': quantities[0],
                'available_qty': max(0.0, quantities[0] - quantities[1]),
            }
            for (lot_id, warehouse_id), quantities in sorted(lot_quantities.items())
            if all(math.isfinite(quantity) for quantity in quantities) and quantities[0] > 0
        ]
        product_rows = [
            {
                'odoo_product_id': product_id,
                'odoo_warehouse_id': warehouse_id,
                'quantity': quantities[0],
                'reserved_quantity': quantities[1],
                'available_quantity': quantities[2],
            }
            for (product_id, warehouse_id), quantities in sorted(product_quantities.items())
            if all(math.isfinite(quantity) for quantity in quantities) and quantities[0] >= 0
        ]
        self._api_request(
            'POST',
            '/api/internal/odoo/lot-stock-snapshot',
            payload={
                'rows': rows, 'product_rows': product_rows, 'reflected_references': [],
                'observed_at': fields.Datetime.now().isoformat() + 'Z',
            },
        )
        return len(product_rows)

    @api.model
    def sync_machines(self):
        rows = self._rest_get('machines', {
            'select': 'id,name,ref,device_imei,device_id_huaxin,state,customer_id',
        })
        Machine = self.env['softlife.machine']
        Partner = self.env['res.partner']
        n = 0
        warnings = []
        for row in rows:
            imei = row.get('device_imei')
            if not imei:
                continue
            vals = {
                'name': row.get('name') or imei,
                'ref': row.get('ref'),
                'device_imei': imei,
                'device_id_huaxin': row.get('device_id_huaxin'),
                'state': row.get('state') or 'active',
            }
            cust = row.get('customer_id')
            if cust:
                partner = Partner.search([('supabase_id', '=', cust)], limit=1)
                if partner:
                    vals['partner_id'] = partner.id
            machine = Machine.search([('device_imei', '=', imei)], limit=1)
            if machine:
                machine.write(vals)
            else:
                machine = Machine.create(vals)

            # Hoppers / ingredients (positions: solid_1..3, liquid_1..3)
            try:
                with self.env.cr.savepoint():
                    ing_rows = self._rest_get('machine_ingredients', {
                        'select': 'position,product_id,product_type,enabled,products(odoo_id)',
                        'machine_id': f'eq.{row.get("id")}',
                    })
                    issues = self._apply_ingredients(machine, ing_rows)
                    warnings.extend(f'{imei} ingredients: {issue}' for issue in issues)
            except Exception as e:
                warnings.append(f'{imei} ingredients: {e}')
                _logger.warning('softlife_sync ingredients for %s: %s', imei, e)
            n += 1
        return (n, warnings) if warnings else n

    @api.model
    def _apply_ingredients(self, machine, ing_rows):
        """Merge Supabase hopper config into Odoo ingredient lines by position
        (preserves portion size / cycled on existing lines; Supabase is source of truth)."""
        Product = self.env['product.product']
        pos_to_line = {ln.position: ln for ln in machine.ingredient_line_ids}
        desired = set()
        issues = []
        for row in ing_rows:
            pos = row.get('position')
            if not pos:
                continue
            vals = {
                'position': pos,
                'product_type': row.get('product_type') or 'topping',
                'enabled': bool(row.get('enabled', True)),
            }
            relation = row.get('products') or {}
            if isinstance(relation, list):
                relation = relation[0] if relation else {}
            odoo_id = relation.get('odoo_id') if isinstance(relation, dict) else None
            product = Product.browse(int(odoo_id)).exists() if odoo_id else Product
            if not product:
                issues.append(f'{pos} has no linked Odoo product')
                continue
            desired.add(pos)
            vals['product_id'] = product.id
            if pos in pos_to_line:
                pos_to_line[pos].write(vals)
            else:
                machine.write({'ingredient_line_ids': [(0, 0, vals)]})
        for pos, ln in pos_to_line.items():
            if pos not in desired:
                ln.unlink()
        return issues

    @api.model
    def sync_orders(self):
        icp = self.env['ir.config_parameter'].sudo()
        since = icp.get_param('softlife.sync.orders_since') or ''
        params = {
            'select': 'order_code,order_time,price,product_name,order_state',
            'order': 'order_time.asc',
        }
        if since:
            params['order_time'] = f'gt.{since}'
        rows = self._rest_get('huaxin_orders', params)

        Move = self.env['account.move']
        pid = self._param('softlife.sync.default_partner_id')
        prod_id = self._param('softlife.sync.default_product_id')
        partner = self.env['res.partner'].browse(int(pid)).exists() if pid else self.env['res.partner']
        product = self.env['product.product'].browse(int(prod_id)).exists() if prod_id else self.env['product.product']
        if not partner or not product:
            _logger.warning(
                'softlife_sync: default partner/product not set; skipping %s order(s)', len(rows)
            )
            return 0
        income_account = (
            product.property_account_income_id or product.categ_id.property_account_income_categ_id
        )

        n = 0
        max_ts = since
        warnings = []
        for row in rows:
            code = row.get('order_code')
            ts = row.get('order_time') or ''
            if ts and ts > max_ts:
                max_ts = ts
            if not code:
                warnings.append(f'order at {ts or "unknown time"}: missing order code')
                continue
            if Move.search([('supabase_order_code', '=', code)], limit=1):
                continue
            try:
                inv_date = (
                    fields.Date.to_date(datetime.datetime.fromisoformat(ts.replace('Z', '+00:00')).date())
                    if ts else fields.Date.today()
                )
            except Exception:
                inv_date = fields.Date.today()
            try:
                with self.env.cr.savepoint():
                    Move.create({
                        'move_type': 'out_invoice',
                        'partner_id': partner.id,
                        'invoice_date': inv_date,
                        'supabase_order_code': code,
                        'invoice_line_ids': [(0, 0, {
                            'product_id': product.id,
                            'name': row.get('product_name') or product.name,
                            'quantity': 1,
                            'price_unit': float(row.get('price') or 0.0),
                            'account_id': income_account.id if income_account else False,
                        })],
                    })
                n += 1
            except Exception as e:
                warnings.append(f'order {code}: {e}')
                _logger.warning('softlife_sync: failed order %s: %s', code, e)

        if not warnings and max_ts and max_ts != since:
            icp.set_param('softlife.sync.orders_since', max_ts)
        return (n, warnings) if warnings else n

    @api.model
    def sync_all(self):
        if not self._is_configured():
            return 'Skipped: Supabase URL / key not configured.'
        if not self._acquire_sync_lock():
            return 'Skipped: another SoftLife full sync is already running.'
        results = {}
        errors = []
        warnings = []
        for name, fn in (('partners', self.sync_partners),
                         ('products', self.sync_products),
                         ('machines', self.sync_machines),
                         ('odoo_warehouses', self.sync_odoo_warehouses),
                         ('odoo_products', self.sync_odoo_products),
                         ('odoo_lots', self.sync_odoo_lots),
                         ('odoo_lot_stock', self.sync_odoo_lot_stock)):
            try:
                with self.env.cr.savepoint():
                    result = fn()
                    if isinstance(result, tuple):
                        results[name] = result[0]
                        warnings.extend(f'{name}: {warning}' for warning in result[1])
                    else:
                        results[name] = result
            except Exception as e:
                results[name] = 0
                errors.append(f'{name}: {e}')
                _logger.exception('softlife_sync %s failed', name)
        def count(name):
            result = results.get(name, 0)
            return result if isinstance(result, int) else 0
        msg = (
            f"Synced {count('partners')} customer(s), "
            f"{count('products')} product(s), "
            f"{count('machines')} machine(s); "
            f"mirrored {count('odoo_products')} Odoo SKU(s), "
            f"{count('odoo_lots')} lot(s), "
            f"{count('odoo_lot_stock')} warehouse-product balance(s), "
            f"{count('odoo_warehouses')} warehouse(s) to Supabase."
        )
        if errors:
            shown = errors[:10]
            if len(errors) > len(shown):
                shown.append(f'{len(errors) - len(shown)} more error(s); see Odoo logs')
            msg += f" Errors: {'; '.join(shown)}"
        if warnings:
            shown = warnings[:10]
            if len(warnings) > len(shown):
                shown.append(f'{len(warnings) - len(shown)} more warning(s); see Odoo logs')
            msg += f" Warnings: {'; '.join(shown)}"
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('softlife.sync.last_sync', fields.Datetime.now())
        icp.set_param('softlife.sync.last_sync_summary', msg)
        return msg

    @api.model
    def process_platform_sync_request(self):
        pending_result = self._retry_pending_platform_sync_result()
        if pending_result:
            return pending_result
        if not self._acquire_sync_lock():
            return False
        claimed = self._api_request('GET', '/api/internal/odoo/sync-requests')
        request = claimed.get('request')
        if not request:
            return False
        request_id = str(request.get('id') or '')
        claim_token = str(request.get('claim_token') or '')
        if not request_id or not claim_token:
            raise SoftlifeAPIError(_('Platform sync request omitted its ID or lease.'), code='invalid_response')
        try:
            result = self._dispatch_platform_sync_request(request)
            result.update({
                'claim_token': claim_token,
                'error': None if result['accepted'] else result.get('error') or result['summary'],
                'finished_at': fields.Datetime.now().isoformat() + 'Z',
            })
        except Exception as exc:
            self.env.cr.rollback()
            self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', [self._sync_lock_id])
            _logger.exception('SoftLife platform-requested sync %s failed', request_id)
            result = {
                'accepted': False, 'summary': 'Platform-requested Odoo operation failed.', 'claim_token': claim_token,
                'error': str(exc), 'finished_at': fields.Datetime.now().isoformat() + 'Z',
            }
        pending = {'request_id': request_id, 'result': result}
        self.env['ir.config_parameter'].sudo().set_param(
            self._pending_sync_result_key, json.dumps(pending),
        )
        self.env.cr.commit()
        return self._retry_pending_platform_sync_result()

    @api.model
    def _dispatch_platform_sync_request(self, request):
        kind = request.get('kind')
        if kind == 'stock_snapshot':
            summary = self.sync_all()
            accepted = not summary.startswith('Skipped:') and ' Errors:' not in summary
            return {'accepted': accepted, 'summary': summary}
        fiscal_handlers = {
            'fiscal_product_remediation': self.remediate_fiscal_products,
            'fiscal_invoice_draft_creation': self._create_fiscal_invoice_drafts,
            'fiscal_invoice_bulk_confirmation': self._confirm_fiscal_invoices,
        }
        if kind in fiscal_handlers:
            payload = request.get('payload')
            payload_hash = request.get('payload_sha256')
            if (
                not isinstance(payload_hash, str)
                or not re.fullmatch(r'[0-9a-f]{64}', payload_hash)
                or self._canonical_sha256(payload) != payload_hash
            ):
                return {
                    'accepted': False,
                    'summary': _('Rejected fiscal operation with an invalid frozen payload hash.'),
                    'error': _('Fiscal operation payload hash mismatch.'),
                }
            return fiscal_handlers[kind](payload)
        return {
            'accepted': False,
            'summary': _('Rejected unsupported platform request kind: %s') % (kind or '<missing>'),
            'error': _('Unsupported platform request kind.'),
        }

    @api.model
    def _retry_pending_platform_sync_result(self):
        icp = self.env['ir.config_parameter'].sudo()
        raw = icp.get_param(self._pending_sync_result_key)
        if not raw:
            return False
        try:
            pending = json.loads(raw)
            request_id = str(pending['request_id'])
            result = pending['result']
        except (KeyError, TypeError, ValueError) as exc:
            raise SoftlifeAPIError(_('Stored platform sync result is invalid.'), code='invalid_response') from exc
        try:
            self._api_request(
                'POST', f'/api/internal/odoo/sync-requests/{request_id}/result', payload=result,
            )
        except SoftlifeAPIError as exc:
            if exc.status != 409:
                raise
            icp.set_param(self._pending_sync_result_key, '')
            self.env.cr.commit()
            return False
        icp.set_param(self._pending_sync_result_key, '')
        self.env.cr.commit()
        return result

    @api.model
    def _cron_sync_requests(self):
        if not self._api_is_configured():
            return
        try:
            self.process_platform_sync_request()
        except Exception as exc:
            _logger.warning('SoftLife sync request polling failed: %s', exc)

    @api.model
    def _cron_fiscal_configuration(self):
        if not self._fiscal_configuration_reporting_enabled() or not self._api_is_configured():
            return
        try:
            self.report_fiscal_configuration()
        except Exception as exc:
            _logger.exception('SoftLife fiscal configuration report failed: %s', exc)
            raise

    @api.model
    def _cron_sync(self):
        try:
            with self.env.cr.savepoint():
                self.sync_all()
        except Exception as e:
            _logger.warning('SoftLife cron sync failed: %s', e)
