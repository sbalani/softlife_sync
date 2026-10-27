import copy
import datetime
import hashlib
import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin

from odoo import Command, _, api, fields, models

from .account_move import _softlife_payout_internal_scope
from .locks import softlife_company_month_advisory_lock
from .softlife_sync_client import SoftlifeAPIError


class SoftlifePayoutSelfBilling(models.TransientModel):
    _inherit = 'softlife.sync.client'

    @api.model
    def _api_binary_post(self, path, content, headers):
        import requests
        base = (self._param('softlife.sync.platform_url') or '').strip().rstrip('/')
        secret = self._param('softlife.sync.odoo_sync_secret')
        if not base or not secret:
            raise SoftlifeAPIError(
                _('Platform App URL / Odoo sync secret not configured.'), code='not_configured',
            )
        try:
            response = requests.post(
                urljoin(f'{base}/', path.lstrip('/')),
                data=content,
                headers={
                    'x-odoo-sync-secret': secret,
                    'Content-Type': 'application/pdf',
                    'Accept': 'application/json',
                    **headers,
                },
                timeout=(10, 60),
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise SoftlifeAPIError(
                _('Platform PDF upload failed: %s') % exc,
                code='network_error', retryable=True,
            ) from exc
        if not 200 <= response.status_code < 300:
            try:
                body = response.json() if response.content else {}
            except ValueError:
                body = {}
            raise SoftlifeAPIError(
                _('Platform PDF upload failed (%s): %s') % (
                    response.status_code,
                    body.get('error') if isinstance(body, dict) and body.get('error')
                    else response.text[:500],
                ),
                status=response.status_code,
                code=body.get('code') if isinstance(body, dict) else None,
                retryable=response.status_code in (408, 425, 429) or response.status_code >= 500,
            )

    @api.model
    def _payout_enabled(self, operation):
        return str(self._param(
            f'softlife.sync.payout_bill_{operation}_enabled', 'False',
        )).lower() in ('1', 'true')

    @api.model
    def _payout_error(self, message):
        raise SoftlifeAPIError(message, code='invalid_payout_self_bill_request')

    @staticmethod
    def _normalized_place(value):
        return ''.join(
            character for character in unicodedata.normalize('NFKD', value or '')
            if not unicodedata.combining(character)
        ).casefold().strip()

    @api.model
    def _payout_account_allowed(self, account, journal, company):
        journal_control_exempt = journal.default_account_id
        if 'suspense_account_id' in journal._fields:
            journal_control_exempt |= journal.suspense_account_id
        return bool(
            account
            and self._account_applies_to_company(account, company)
            and not ('deprecated' in account._fields and account.deprecated)
            and (not account.currency_id or account.currency_id == company.currency_id)
        ) and not (
            'allowed_journal_ids' in account._fields and account.allowed_journal_ids
            and journal not in account.allowed_journal_ids
        ) and not (
            'account_control_ids' in journal._fields and journal.account_control_ids
            and account not in journal.account_control_ids
            and account not in journal_control_exempt
        )

    @api.model
    def _payout_configuration(self, configured_company=None):
        journal_domain = [('code', '=', 'AFRS'), ('type', '=', 'purchase')]
        if configured_company:
            configured_company_id = configured_company.id
        else:
            try:
                configured_company_id = int(self._param('softlife.sync.fiscal_company_id'))
            except (TypeError, ValueError):
                configured_company_id = 0
        if configured_company_id:
            journal_domain.append(('company_id', '=', configured_company_id))
        journals = self.env['account.journal'].sudo().with_context(active_test=False).search(
            journal_domain
        )
        if len(journals) != 1 or not journals.active:
            self._payout_error(_(
                'Odoo must contain exactly one active AFRS purchase journal.'
            ))
        journal = journals
        company = journal.company_id
        Account = self.env['account.account'].sudo().with_company(company).with_context(
            active_test=False, allowed_company_ids=[company.id]
        )
        account_domain = [
            ('code', '=', '623100'), ('account_type', 'in', ('expense', 'expense_direct_cost')),
        ]
        if 'company_ids' in Account._fields:
            account_domain.append(('company_ids', 'in', [company.id]))
        elif 'company_id' in Account._fields:
            account_domain.append(('company_id', 'in', [False, company.id]))
        accounts = Account.search(account_domain).filtered(
            lambda candidate: self._account_applies_to_company(candidate, company)
        )
        if len(accounts) != 1:
            self._payout_error(_(
                'Odoo must contain exactly one expense account 623100 for the AFRS company.'
            ))
        account = accounts
        tax = self.env.ref(
            f'account.{company.id}_account_tax_template_p_iva21_sc',
            raise_if_not_found=False,
        )
        if not tax or tax._name != 'account.tax':
            self._payout_error(_(
                'The standard Spanish domestic 21% service purchase tax (21% S) is missing '
                'for the AFRS company.'
            ))
        tax = tax.sudo().with_company(company)
        repartition = tax.invoice_repartition_line_ids
        base_repartition = repartition.filtered(lambda line: line.repartition_type == 'base')
        tax_repartition = repartition.filtered(lambda line: line.repartition_type == 'tax')
        valid_repartition = (
            sum(base_repartition.mapped('factor_percent')) == 100
            and sum(tax_repartition.mapped('factor_percent')) == 100
            and tax_repartition
            and all(line.account_id and line.account_id.account_type == 'asset_current'
                    and self._payout_account_allowed(line.account_id, journal, company)
                    for line in tax_repartition)
            and any(line.tag_ids for line in base_repartition)
            and any(line.tag_ids for line in tax_repartition)
        )
        if (
            not company.active or company.currency_id.name != 'EUR'
            or not company.account_fiscal_country_id
            or company.account_fiscal_country_id.code != 'ES'
            or not journal.active or journal.company_id != company or journal.type != 'purchase'
            or ('is_self_billing' in journal._fields and not journal.is_self_billing)
            or not self._payout_account_allowed(account, journal, company)
            or not tax.active or tax.company_id != company
            or tax.type_tax_use != 'purchase' or tax.amount_type != 'percent'
            or tax.amount != 21 or tax.price_include
            or tax.tax_scope != 'service'
            or tax.l10n_es_type != 'sujeto' or tax.l10n_es_bien_inversion
            or tax.country_id != company.account_fiscal_country_id
            or tax.tax_exigibility != 'on_invoice' or not valid_repartition
        ):
            self._payout_error(_(
                'The AFRS journal must use a Spanish EUR company, an unrestricted active account '
                '623100, and the standard excluded Spanish domestic 21% service purchase tax '
                '(21% S).'
            ))
        return company, journal, account, tax

    @api.model
    def _prepare_payout_vendor(self, bill, company, journal):
        vendor = bill.get('vendor')
        expected_fields = {
            'legal_name', 'tax_id', 'email', 'address_line_1', 'address_line_2',
            'postal_code', 'city', 'province', 'country_code',
        }
        if not isinstance(vendor, dict) or set(vendor) != expected_fields:
            self._payout_error(_('Payout vendor fields are invalid.'))
        required = expected_fields - {'address_line_2'}
        if (
            any(not isinstance(vendor[name], str) or not vendor[name].strip() for name in required)
            or vendor['address_line_2'] is not None
            and not isinstance(vendor['address_line_2'], str)
            or vendor['country_code'] != 'ES'
            or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', vendor['email'])
        ):
            self._payout_error(_('Payout vendor legal identity is invalid.'))
        limits = {
            'legal_name': 200, 'tax_id': 50, 'email': 254, 'address_line_1': 300,
            'address_line_2': 300, 'postal_code': 20, 'city': 100,
            'province': 100, 'country_code': 2,
        }
        if any(vendor[name] is not None and len(vendor[name]) > limit
               for name, limit in limits.items()):
            self._payout_error(_('Payout vendor legal identity is invalid.'))
        partners = self.env['res.partner'].sudo().with_context(active_test=False).search([
            ('supabase_id', '=', bill['tenant_id']),
        ])
        if len(partners) != 1:
            self._payout_error(_(
                'The payout vendor must already exist exactly once with the tenant supabase_id.'
            ))
        partner = partners
        if partner and partner.company_id and partner.company_id != company:
            self._payout_error(_('Payout vendor belongs to another Odoo company.'))
        company_partner = partner.with_company(company)
        payable_account = company_partner.property_account_payable_id
        fiscal_position = self.env['account.fiscal.position'].sudo().with_company(
            company
        )._get_fiscal_position(company_partner)
        if fiscal_position:
            payable_account = fiscal_position.map_account(payable_account)
        if (
            payable_account.account_type != 'liability_payable'
            or not self._payout_account_allowed(payable_account, journal, company)
        ):
            self._payout_error(_('Payout vendor has no valid payable account for the AFRS journal.'))
        self._validate_payout_vendor_identity(partner, bill)
        return partner

    @api.model
    def _validate_payout_vendor_identity(self, partner, bill):
        vendor = bill.get('vendor') if isinstance(bill, dict) else None
        if not isinstance(vendor, dict) or set(vendor) != {
                'legal_name', 'tax_id', 'email', 'address_line_1', 'address_line_2',
                'postal_code', 'city', 'province', 'country_code'}:
            self._payout_error(_('Stored payout vendor identity is invalid.'))
        actual = {
            'legal_name': partner.name, 'tax_id': partner.vat or '',
            'email': partner.email or '', 'address_line_1': partner.street or '',
            'address_line_2': partner.street2 or None, 'postal_code': partner.zip or '',
            'city': partner.city or '', 'province': vendor['province'],
            'country_code': partner.country_id.code or '',
        }
        if (
            actual != vendor
            or self._normalized_place(vendor['province']) not in {
                self._normalized_place(partner.state_id.name),
                self._normalized_place(partner.state_id.code),
            }
            or partner.supabase_id != bill['tenant_id']
            or not partner.active or partner.company_type != 'company'
        ):
            self._payout_error(_('Payout vendor does not match the frozen legal identity.'))

    @api.model
    def _validate_payout_draft_payload(self, payload, payload_sha256):
        if not isinstance(payload, dict) or set(payload) != {'schema_version', 'bill'} \
                or payload.get('schema_version') != 1 or isinstance(payload.get('schema_version'), bool):
            self._payout_error(_('Payout self-bill draft payload fields are invalid.'))
        if not isinstance(payload_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', payload_sha256):
            self._payout_error(_('Payout self-bill payload hash is invalid.'))
        bill = payload['bill']
        expected = {
            'platform_bill_id', 'settlement_id', 'tenant_id', 'period_month', 'invoice_date',
            'currency', 'move_type', 'reference', 'description', 'gross_cents',
            'tax_base_cents', 'iva_cents', 'iva_rate_percent', 'contract_acceptance_id',
            'contract_version', 'source_snapshot_sha256', 'vendor',
        }
        if not isinstance(bill, dict) or set(bill) != expected:
            self._payout_error(_('Payout self-bill fields are invalid.'))
        if any(not self._canonical_uuid(bill.get(name)) for name in (
                'platform_bill_id', 'settlement_id', 'tenant_id', 'contract_acceptance_id')):
            self._payout_error(_('Payout self-bill identity is invalid.'))
        try:
            month_start = datetime.date.fromisoformat(str(bill.get('period_month')) + '-01')
            next_month = (month_start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
            invoice_date = datetime.date.fromisoformat(str(bill.get('invoice_date')))
        except (TypeError, ValueError):
            self._payout_error(_('Payout self-bill invoice date is invalid.'))
        if (
            not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', str(bill.get('period_month')))
            or invoice_date != next_month or bill['invoice_date'] != next_month.isoformat()
            or bill.get('currency') != 'EUR' or bill.get('move_type') != 'in_invoice'
            or not isinstance(bill.get('reference'), str) or not bill['reference'].strip()
            or len(bill['reference']) > 200
            or bill['reference'] != f'AUTOFAC-{bill["period_month"]}-{bill["tenant_id"]}'
            or not isinstance(bill.get('description'), str)
            or 'facturación por el destinatario' not in bill['description']
            or bill.get('contract_version') != 'SL-GESTOR-2026-10-27-v4'
            or not re.fullmatch(r'[0-9a-f]{64}', str(bill.get('source_snapshot_sha256') or ''))
            or not self._positive_int(bill.get('gross_cents'))
            or not self._nonnegative_int(bill.get('tax_base_cents'))
            or not self._nonnegative_int(bill.get('iva_cents'))
            or bill.get('iva_rate_percent') != 21
            or isinstance(bill.get('iva_rate_percent'), bool)
        ):
            self._payout_error(_('Payout self-bill values are invalid.'))
        computed_iva = (Decimal(bill['tax_base_cents']) * Decimal(21) / Decimal(100)).quantize(
            Decimal('1'), rounding=ROUND_HALF_UP,
        )
        if (
            bill['tax_base_cents'] + bill['iva_cents'] != bill['gross_cents']
            or computed_iva != bill['iva_cents']
        ):
            self._payout_error(_('Payout self-bill tax-base IVA cents are invalid.'))
        company, journal, account, tax = self._payout_configuration()
        accounting_key = f'payout:{company.id}:{bill["tenant_id"]}:{bill["period_month"]}'
        due_date = next_month.replace(day=15)
        return bill, company, journal, account, tax, accounting_key, due_date

    @api.model
    def _payout_recipient_snapshot(self, company):
        values = {
            'legal_name': company.name or '', 'tax_id': company.vat or '',
            'address_line_1': company.street or '', 'address_line_2': company.street2 or None,
            'postal_code': company.zip or '', 'city': company.city or '',
            'province': company.state_id.name or '',
            'country_code': company.country_id.code or '',
        }
        required = set(values) - {'address_line_2'}
        if any(not isinstance(values[name], str) or not values[name].strip()
               for name in required) or values['country_code'] != 'ES':
            self._payout_error(_(
                'Payout self-billing company legal name, VAT, and Spanish postal address are required.'
            ))
        return values, self._canonical_sha256(values)

    @api.model
    def _assert_payout_date_unlocked(self, company, journal, invoice_date):
        violations = company._get_violated_lock_dates(invoice_date, True, journal)
        if violations:
            self._payout_error(_(
                'Payout self-bill date %(date)s is in a locked accounting period: %(reason)s',
                date=fields.Date.to_string(invoice_date),
                reason=company._format_lock_dates(violations),
            ))

    @api.model
    def _validate_payout_move(self, move, bill, payload_sha256, company, journal,
                              account, tax, accounting_key, due_date, partner=None):
        partner = partner or move.partner_id
        if (
            move.softlife_payout_bill_id != bill['platform_bill_id']
            or move.softlife_payout_settlement_id != bill['settlement_id']
            or move.softlife_payout_tenant_id != bill['tenant_id']
            or move.softlife_payout_period_month != bill['period_month']
            or move.softlife_payout_payload_sha256 != payload_sha256
            or move.softlife_payout_source_snapshot_sha256 != bill['source_snapshot_sha256']
            or move.softlife_payout_bill_snapshot != bill
            or move.softlife_payout_accounting_key != accounting_key
            or move.company_id != company or move.journal_id != journal
            or move.partner_id != partner or move.move_type != 'in_invoice'
            or move.currency_id != company.currency_id
            or fields.Date.to_string(move.invoice_date) != bill['invoice_date']
            or fields.Date.to_string(move.date) != bill['invoice_date']
            or fields.Date.to_string(move.invoice_date_due) != fields.Date.to_string(due_date)
            or move.invoice_payment_term_id
            or move.ref != bill['reference'] or move.state not in ('draft', 'posted')
            or 'facturación por el destinatario' not in (move.narration or '')
        ):
            self._payout_error(_('Existing payout self-bill does not match its frozen payload.'))
        self._validate_payout_vendor_identity(partner, bill)
        recipient_hash = self._canonical_sha256(move.softlife_payout_recipient_snapshot)
        if (
            not isinstance(move.softlife_payout_recipient_snapshot, dict)
            or recipient_hash != move.softlife_payout_recipient_sha256
        ):
            self._payout_error(_('Payout self-bill recipient snapshot is invalid.'))
        lines = move.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
        if len(lines) != 1:
            self._payout_error(_('Payout self-bill must contain exactly one aggregate line.'))
        line = lines[0]
        if (
            line.product_id or line.name != bill['description'] or line.quantity != 1
            or abs(line.price_unit - float(Decimal(bill['tax_base_cents']) / Decimal(100))) > 1e-9
            or line.account_id != account or line.tax_ids != tax
            or self._amount_cents(line.price_total) != bill['gross_cents']
            or self._amount_cents(line.price_subtotal) != bill['tax_base_cents']
            or self._amount_cents(move.amount_total) != bill['gross_cents']
            or self._amount_cents(move.amount_untaxed) != bill['tax_base_cents']
            or self._amount_cents(move.amount_tax) != bill['iva_cents']
        ):
            self._payout_error(_('Payout self-bill accounting amounts do not match frozen cents.'))
        if move.state == 'posted':
            payable_lines = move.line_ids.filtered(
                lambda item: item.account_id.account_type == 'liability_payable'
            )
            if not payable_lines or any(
                    fields.Date.to_string(item.date_maturity) != fields.Date.to_string(due_date)
                    for item in payable_lines):
                self._payout_error(_('Payout self-bill payable maturity does not match the due date.'))

    @api.model
    def _create_payout_vendor_bill_draft(self, payload, payload_sha256):
        if not self._payout_enabled('draft_creation'):
            raise SoftlifeAPIError(
                _('Payout self-bill draft creation is disabled in Odoo Settings.'),
                code='payout_vendor_bill_draft_creation_disabled',
            )
        bill, company, journal, account, tax, accounting_key, due_date = \
            self._validate_payout_draft_payload(payload, payload_sha256)
        invoice_date = fields.Date.to_date(bill['invoice_date'])
        self._assert_payout_date_unlocked(company, journal, invoice_date)
        softlife_company_month_advisory_lock(self.env.cr, company.id, invoice_date.replace(day=1))
        existing = self.env['account.move'].sudo().with_context(active_test=False).search([
            '|', ('softlife_payout_bill_id', '=', bill['platform_bill_id']),
            ('softlife_payout_accounting_key', '=', accounting_key),
        ])
        if existing:
            self.env.cr.execute(
                'SELECT id FROM account_move WHERE id IN %s FOR UPDATE', [tuple(existing.ids)],
            )
            existing.invalidate_recordset()
        if len(existing) > 1 or existing and (
                existing.softlife_payout_bill_id != bill['platform_bill_id']
                or existing.softlife_payout_accounting_key != accounting_key):
            self._payout_error(_('Payout self-bill identity conflicts with an existing bill.'))
        partner = self._prepare_payout_vendor(bill, company, journal)
        recipient, recipient_hash = self._payout_recipient_snapshot(company)
        move = existing
        created = not move
        if not move:
            with _softlife_payout_internal_scope():
                move = self.env['account.move'].sudo().with_company(company).create({
                    'company_id': company.id, 'journal_id': journal.id,
                    'partner_id': partner.id, 'move_type': 'in_invoice',
                    'currency_id': company.currency_id.id, 'date': invoice_date,
                    'invoice_date': invoice_date, 'invoice_payment_term_id': False,
                    'invoice_date_due': due_date, 'ref': bill['reference'],
                    'narration': bill['description'],
                    'invoice_line_ids': [Command.create({
                        'name': bill['description'], 'quantity': 1,
                        'price_unit': float(Decimal(bill['tax_base_cents']) / Decimal(100)),
                        'account_id': account.id, 'tax_ids': [Command.set(tax.ids)],
                    })],
                    'softlife_payout_bill_id': bill['platform_bill_id'],
                    'softlife_payout_settlement_id': bill['settlement_id'],
                    'softlife_payout_tenant_id': bill['tenant_id'],
                    'softlife_payout_period_month': bill['period_month'],
                    'softlife_payout_payload_sha256': payload_sha256,
                    'softlife_payout_source_snapshot_sha256': bill['source_snapshot_sha256'],
                    'softlife_payout_bill_snapshot': copy.deepcopy(bill),
                    'softlife_payout_accounting_key': accounting_key,
                    'softlife_payout_recipient_snapshot': recipient,
                    'softlife_payout_recipient_sha256': recipient_hash,
                })
        self._validate_payout_move(
            move, bill, payload_sha256, company, journal, account, tax,
            accounting_key, due_date, partner,
        )
        if move.state != 'draft':
            self._payout_error(_('Payout draft creation cannot reuse a posted self-bill.'))
        return {'accepted': True, 'bill': {
            'platform_bill_id': bill['platform_bill_id'], 'odoo_move_id': move.id,
            'odoo_partner_id': partner.id, 'odoo_journal_id': journal.id,
            'payload_sha256': payload_sha256, 'state': 'draft', 'created': created,
            'amount_total_cents': self._amount_cents(move.amount_total),
        }}

    @api.model
    def _post_payout_vendor_bill(self, payload):
        if not self._payout_enabled('posting'):
            raise SoftlifeAPIError(
                _('Payout self-bill posting is disabled in Odoo Settings.'),
                code='payout_vendor_bill_posting_disabled',
            )
        if not isinstance(payload, dict) or set(payload) != {'schema_version', 'bill'} \
                or payload.get('schema_version') != 1 or isinstance(payload.get('schema_version'), bool):
            self._payout_error(_('Payout self-bill posting payload fields are invalid.'))
        bill = payload['bill']
        if not isinstance(bill, dict) or set(bill) != {
                'platform_bill_id', 'odoo_move_id', 'payload_sha256'} \
                or not self._canonical_uuid(bill.get('platform_bill_id')) \
                or not self._positive_int(bill.get('odoo_move_id')) \
                or not re.fullmatch(r'[0-9a-f]{64}', str(bill.get('payload_sha256') or '')):
            self._payout_error(_('Payout self-bill posting identity is invalid.'))
        self.env.cr.execute(
            'SELECT id FROM account_move WHERE id = %s FOR UPDATE', [bill['odoo_move_id']],
        )
        if not self.env.cr.fetchone():
            self._payout_error(_('Payout self-bill move does not exist.'))
        move = self.env['account.move'].sudo().browse(bill['odoo_move_id'])
        move.invalidate_recordset()
        snapshot = move.softlife_payout_bill_snapshot
        if (
            not isinstance(snapshot, dict)
            or move.softlife_payout_bill_id != bill['platform_bill_id']
            or move.softlife_payout_payload_sha256 != bill['payload_sha256']
        ):
            self._payout_error(_('Payout self-bill posting identity does not match Odoo.'))
        if move.state == 'posted':
            lines = move.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
            if len(lines) != 1 or not move.name:
                self._payout_error(_('Posted payout self-bill is structurally inconsistent.'))
            company, journal, account, tax = (
                move.company_id, move.journal_id, lines.account_id, lines.tax_ids,
            )
            accounting_key = move.softlife_payout_accounting_key
            due_date = fields.Date.to_date(snapshot.get('invoice_date')).replace(day=15)
        else:
            company, journal, account, tax = self._payout_configuration()
            accounting_key = (
                f'payout:{company.id}:{snapshot.get("tenant_id")}:{snapshot.get("period_month")}'
            )
            self._assert_payout_date_unlocked(
                company, journal, fields.Date.to_date(snapshot.get('invoice_date')),
            )
            due_date = fields.Date.to_date(snapshot.get('invoice_date')).replace(day=15)
        self._validate_payout_move(
            move, snapshot, bill['payload_sha256'], company, journal, account, tax,
            accounting_key, due_date,
        )
        posted = move.state == 'draft'
        if posted:
            with _softlife_payout_internal_scope():
                move.action_post()
            if fields.Date.to_string(move.date) != snapshot['invoice_date']:
                self._payout_error(_('Odoo changed the payout self-bill accounting date.'))
            payable_lines = move.line_ids.filtered(
                lambda line: line.account_id.account_type == 'liability_payable'
            )
            if not payable_lines or any(
                    fields.Date.to_string(line.date_maturity) != fields.Date.to_string(due_date)
                    for line in payable_lines):
                self._payout_error(_('Odoo changed the payout self-bill due date.'))
        return {'accepted': True, 'bill': {
            'platform_bill_id': bill['platform_bill_id'], 'odoo_move_id': move.id,
            'payload_sha256': bill['payload_sha256'], 'state': 'posted',
            'name': move.name, 'posted': posted,
        }}

    @api.model
    def _payout_invoice_report(self):
        report = self.env.ref('account.account_invoices', raise_if_not_found=False)
        if (
            report and report._name == 'ir.actions.report'
            and report.model == 'account.move' and report.report_type == 'qweb-pdf'
        ):
            return report
        self._payout_error(_('The standard Odoo invoice PDF report is unavailable.'))

    @api.model
    def _export_payout_vendor_bill_pdf(self, payload, request):
        if not self._payout_enabled('pdf_export'):
            raise SoftlifeAPIError(
                _('Payout self-bill PDF export is disabled in Odoo Settings.'),
                code='payout_vendor_bill_pdf_export_disabled',
            )
        if not isinstance(payload, dict) or set(payload) != {'schema_version', 'bill'} \
                or payload.get('schema_version') != 1 or isinstance(payload.get('schema_version'), bool):
            self._payout_error(_('Payout self-bill PDF payload fields are invalid.'))
        bill = payload['bill']
        if not isinstance(bill, dict) or set(bill) != {
                'platform_bill_id', 'odoo_move_id', 'payload_sha256', 'odoo_name'} \
                or not self._canonical_uuid(bill.get('platform_bill_id')) \
                or not self._positive_int(bill.get('odoo_move_id')) \
                or not re.fullmatch(r'[0-9a-f]{64}', str(bill.get('payload_sha256') or '')) \
                or not isinstance(bill.get('odoo_name'), str) or not bill['odoo_name']:
            self._payout_error(_('Payout self-bill PDF identity is invalid.'))
        move = self.env['account.move'].sudo().browse(bill['odoo_move_id']).exists()
        if (
            not move or move.state != 'posted' or move.move_type != 'in_invoice'
            or move.softlife_payout_bill_id != bill['platform_bill_id']
            or move.softlife_payout_payload_sha256 != bill['payload_sha256']
            or move.name != bill['odoo_name']
        ):
            self._payout_error(_('Payout self-bill PDF move does not match the posted Odoo bill.'))
        snapshot = move.softlife_payout_bill_snapshot
        lines = move.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
        if not isinstance(snapshot, dict) or len(lines) != 1:
            self._payout_error(_('Posted payout self-bill is structurally inconsistent.'))
        self._validate_payout_move(
            move, snapshot, bill['payload_sha256'], move.company_id, move.journal_id,
            lines.account_id, lines.tax_ids, move.softlife_payout_accounting_key,
            fields.Date.to_date(snapshot.get('invoice_date')).replace(day=15),
        )
        attachments = self.env['ir.attachment'].sudo().search([
            ('softlife_payout_bill_id', '=', bill['platform_bill_id']),
        ])
        if len(attachments) > 1:
            self._payout_error(_('Payout self-bill has more than one PDF attachment.'))
        attachment = attachments
        if attachment:
            pdf = attachment.raw or b''
            digest = hashlib.sha256(pdf).hexdigest()
            if (
                attachment.res_model != 'account.move' or attachment.res_id != move.id
                or attachment.mimetype != 'application/pdf'
                or attachment.softlife_payout_pdf_sha256 != digest or not pdf
            ):
                self._payout_error(_('Stored payout self-bill PDF attachment is inconsistent.'))
        else:
            report = self._payout_invoice_report()
            pdf, _content_type = report._render_qweb_pdf(report.report_name, res_ids=move.ids)
            if not isinstance(pdf, bytes) or not pdf.startswith(b'%PDF'):
                self._payout_error(_('Odoo returned an invalid payout self-bill PDF.'))
            digest = hashlib.sha256(pdf).hexdigest()
            with _softlife_payout_internal_scope():
                attachment = self.env['ir.attachment'].sudo().create({
                    'name': f'{move.name.replace("/", "-")}.pdf',
                    'type': 'binary', 'raw': pdf,
                    'mimetype': 'application/pdf', 'res_model': 'account.move',
                    'res_id': move.id, 'softlife_payout_bill_id': bill['platform_bill_id'],
                    'softlife_payout_pdf_sha256': digest,
                })
            # Persist the exact bytes before the external upload so an uncertain response
            # can only retry this immutable attachment, never rerender another PDF.
            self.env.cr.commit()
        self._api_binary_post(
            f'/api/internal/odoo/payout-self-bills/{bill["platform_bill_id"]}/pdf', pdf,
            headers={
                'x-odoo-request-id': str(request.get('id') or ''),
                'x-odoo-claim-token': str(request.get('claim_token') or ''),
                'x-odoo-move-id': str(move.id),
                'x-odoo-attachment-id': str(attachment.id),
                'x-pdf-sha256': digest,
            },
        )
        return {'accepted': True, 'bill': {
            'platform_bill_id': bill['platform_bill_id'], 'odoo_move_id': move.id,
            'payload_sha256': bill['payload_sha256'], 'state': 'posted',
            'odoo_attachment_id': attachment.id, 'pdf_sha256': digest,
            'pdf_size_bytes': len(pdf),
        }}
