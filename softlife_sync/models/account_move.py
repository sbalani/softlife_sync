from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_FISCAL_AUTHORITY = object()
_fiscal_authority = ContextVar('softlife_fiscal_authority', default=None)
_PAYOUT_AUTHORITY = object()
_payout_authority = ContextVar('softlife_payout_authority', default=None)


@contextmanager
def _softlife_fiscal_internal_scope():
    token = _fiscal_authority.set(_FISCAL_AUTHORITY)
    try:
        yield
    finally:
        _fiscal_authority.reset(token)


def _softlife_fiscal_is_authorized():
    return _fiscal_authority.get() is _FISCAL_AUTHORITY


@contextmanager
def _softlife_payout_internal_scope():
    token = _payout_authority.set(_PAYOUT_AUTHORITY)
    try:
        yield
    finally:
        _payout_authority.reset(token)


def _softlife_payout_is_authorized():
    return _payout_authority.get() is _PAYOUT_AUTHORITY


class AccountMove(models.Model):
    _inherit = 'account.move'

    supabase_order_code = fields.Char(string='SoftLife Order #', index=True, copy=False)
    softlife_fiscal_invoice_id = fields.Char(
        string='SoftLife Fiscal Invoice ID', index=True, copy=False,
    )
    softlife_fiscal_payload_sha256 = fields.Char(
        string='SoftLife Fiscal Payload SHA-256', copy=False,
    )
    softlife_fiscal_invoice_snapshot = fields.Json(
        string='SoftLife Fiscal Invoice Snapshot', copy=False,
    )
    softlife_fiscal_accounting_key = fields.Char(index=True, copy=False)
    softlife_payout_bill_id = fields.Char(index=True, copy=False)
    softlife_payout_settlement_id = fields.Char(index=True, copy=False)
    softlife_payout_tenant_id = fields.Char(index=True, copy=False)
    softlife_payout_period_month = fields.Char(index=True, copy=False)
    softlife_payout_payload_sha256 = fields.Char(copy=False)
    softlife_payout_source_snapshot_sha256 = fields.Char(copy=False)
    softlife_payout_bill_snapshot = fields.Json(copy=False)
    softlife_payout_accounting_key = fields.Char(index=True, copy=False)
    softlife_payout_recipient_snapshot = fields.Json(copy=False)
    softlife_payout_recipient_sha256 = fields.Char(copy=False)

    _sql_constraints = [
        ('softlife_fiscal_invoice_id_unique', 'unique(softlife_fiscal_invoice_id)',
         'A SoftLife fiscal invoice can only exist once.'),
        ('softlife_fiscal_accounting_key_unique', 'unique(softlife_fiscal_accounting_key)',
         'This SoftLife fiscal accounting scope has already been invoiced.'),
        ('softlife_fiscal_provenance_paired',
         "CHECK ((softlife_fiscal_invoice_id IS NULL AND "
         "softlife_fiscal_payload_sha256 IS NULL AND softlife_fiscal_invoice_snapshot IS NULL) OR "
         "(softlife_fiscal_invoice_id IS NOT NULL AND "
         "softlife_fiscal_payload_sha256 IS NOT NULL AND softlife_fiscal_invoice_snapshot IS NOT NULL))",
         'SoftLife fiscal invoice provenance must be stored together.'),
        ('softlife_fiscal_invoice_id_format',
         "CHECK (softlife_fiscal_invoice_id IS NULL OR softlife_fiscal_invoice_id ~ "
         "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')",
         'The SoftLife fiscal invoice ID must be a canonical UUID.'),
        ('softlife_fiscal_payload_sha256_format',
         "CHECK (softlife_fiscal_payload_sha256 IS NULL OR "
         "softlife_fiscal_payload_sha256 ~ '^[0-9a-f]{64}$')",
         'The SoftLife fiscal invoice payload hash must be lowercase SHA-256.'),
        ('softlife_payout_bill_id_unique', 'unique(softlife_payout_bill_id)',
         'A SoftLife payout self-bill can only exist once.'),
        ('softlife_payout_accounting_key_unique', 'unique(softlife_payout_accounting_key)',
         'This SoftLife payout accounting scope has already been billed.'),
        ('softlife_payout_bill_id_format',
         "CHECK (softlife_payout_bill_id IS NULL OR softlife_payout_bill_id ~ "
         "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')",
         'The SoftLife payout bill ID must be a canonical UUID.'),
        ('softlife_payout_hashes_format',
         "CHECK ((softlife_payout_payload_sha256 IS NULL OR softlife_payout_payload_sha256 ~ "
         "'^[0-9a-f]{64}$') AND (softlife_payout_source_snapshot_sha256 IS NULL OR "
         "softlife_payout_source_snapshot_sha256 ~ '^[0-9a-f]{64}$'))",
         'SoftLife payout hashes must be lowercase SHA-256.'),
        ('softlife_payout_provenance_paired',
         "CHECK ((softlife_payout_bill_id IS NULL AND softlife_payout_settlement_id IS NULL "
         "AND softlife_payout_tenant_id IS NULL AND softlife_payout_period_month IS NULL "
         "AND softlife_payout_payload_sha256 IS NULL AND softlife_payout_source_snapshot_sha256 IS NULL "
         "AND softlife_payout_bill_snapshot IS NULL AND softlife_payout_accounting_key IS NULL "
         "AND softlife_payout_recipient_snapshot IS NULL AND softlife_payout_recipient_sha256 IS NULL) OR "
         "(softlife_payout_bill_id IS NOT NULL AND softlife_payout_settlement_id IS NOT NULL "
         "AND softlife_payout_tenant_id IS NOT NULL AND softlife_payout_period_month IS NOT NULL "
         "AND softlife_payout_payload_sha256 IS NOT NULL AND softlife_payout_source_snapshot_sha256 IS NOT NULL "
         "AND softlife_payout_bill_snapshot IS NOT NULL AND softlife_payout_accounting_key IS NOT NULL "
         "AND softlife_payout_recipient_snapshot IS NOT NULL AND softlife_payout_recipient_sha256 IS NOT NULL))",
         'SoftLife payout self-bill provenance must be stored together.'),
    ]

    @api.constrains(
        'softlife_fiscal_invoice_id', 'softlife_fiscal_payload_sha256',
        'softlife_fiscal_invoice_snapshot',
    )
    def _check_softlife_fiscal_provenance(self):
        for move in self:
            values = (
                move.softlife_fiscal_invoice_id,
                move.softlife_fiscal_payload_sha256,
                move.softlife_fiscal_invoice_snapshot,
            )
            if any(values) and not all(values):
                raise ValidationError(_('SoftLife fiscal invoice provenance must be stored together.'))
            if all(values) and (
                not isinstance(move.softlife_fiscal_invoice_snapshot, dict)
                or move.softlife_fiscal_invoice_snapshot.get('platform_invoice_id')
                != move.softlife_fiscal_invoice_id
                or move.softlife_fiscal_invoice_snapshot.get('invoice_payload_sha256')
                != move.softlife_fiscal_payload_sha256
            ):
                raise ValidationError(_('SoftLife fiscal invoice provenance is inconsistent.'))

    @api.constrains(
        'softlife_payout_bill_id', 'softlife_payout_settlement_id',
        'softlife_payout_tenant_id', 'softlife_payout_period_month',
        'softlife_payout_payload_sha256', 'softlife_payout_source_snapshot_sha256',
        'softlife_payout_bill_snapshot', 'softlife_payout_accounting_key',
        'softlife_payout_recipient_snapshot', 'softlife_payout_recipient_sha256',
    )
    def _check_softlife_payout_provenance(self):
        for move in self:
            values = (
                move.softlife_payout_bill_id, move.softlife_payout_settlement_id,
                move.softlife_payout_tenant_id, move.softlife_payout_period_month,
                move.softlife_payout_payload_sha256,
                move.softlife_payout_source_snapshot_sha256,
                move.softlife_payout_bill_snapshot, move.softlife_payout_accounting_key,
                move.softlife_payout_recipient_snapshot, move.softlife_payout_recipient_sha256,
            )
            if any(values) and not all(values):
                raise ValidationError(_(
                    'SoftLife payout self-bill provenance must be stored together.'
                ))
            snapshot = move.softlife_payout_bill_snapshot
            payload_hash = hashlib.sha256(json.dumps(
                {'schema_version': 1, 'bill': snapshot}, sort_keys=True,
                separators=(',', ':'), ensure_ascii=False,
            ).encode()).hexdigest() if isinstance(snapshot, dict) else None
            recipient = move.softlife_payout_recipient_snapshot
            recipient_hash = hashlib.sha256(json.dumps(
                recipient, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            ).encode()).hexdigest() if isinstance(recipient, dict) else None
            if all(values) and (
                not isinstance(snapshot, dict)
                or snapshot.get('platform_bill_id') != move.softlife_payout_bill_id
                or snapshot.get('settlement_id') != move.softlife_payout_settlement_id
                or snapshot.get('tenant_id') != move.softlife_payout_tenant_id
                or snapshot.get('period_month') != move.softlife_payout_period_month
                or snapshot.get('source_snapshot_sha256')
                != move.softlife_payout_source_snapshot_sha256
                or payload_hash != move.softlife_payout_payload_sha256
                or recipient_hash != move.softlife_payout_recipient_sha256
            ):
                raise ValidationError(_('SoftLife payout self-bill provenance is inconsistent.'))

    @api.model_create_multi
    def create(self, vals_list):
        provenance = {
            'softlife_fiscal_invoice_id', 'softlife_fiscal_payload_sha256',
            'softlife_fiscal_invoice_snapshot', 'softlife_fiscal_accounting_key',
        }
        if not _softlife_fiscal_is_authorized() and any(
                provenance.intersection(vals) for vals in vals_list):
            raise UserError(_('SoftLife fiscal invoice provenance is managed internally.'))
        payout_provenance = {
            'softlife_payout_bill_id', 'softlife_payout_settlement_id',
            'softlife_payout_tenant_id', 'softlife_payout_period_month',
            'softlife_payout_payload_sha256', 'softlife_payout_source_snapshot_sha256',
            'softlife_payout_bill_snapshot', 'softlife_payout_accounting_key',
            'softlife_payout_recipient_snapshot', 'softlife_payout_recipient_sha256',
        }
        if not _softlife_payout_is_authorized() and any(
                payout_provenance.intersection(vals) for vals in vals_list):
            raise UserError(_('SoftLife payout self-bill provenance is managed internally.'))
        return super().create(vals_list)

    def write(self, vals):
        provenance = {
            'softlife_fiscal_invoice_id', 'softlife_fiscal_payload_sha256',
            'softlife_fiscal_invoice_snapshot', 'softlife_fiscal_accounting_key',
        }
        protected_draft_fields = {
            'company_id', 'journal_id', 'partner_id', 'move_type', 'currency_id',
            'date', 'invoice_date', 'invoice_date_due', 'invoice_payment_term_id',
            'fiscal_position_id', 'payment_reference', 'ref', 'invoice_line_ids',
        }
        if not _softlife_fiscal_is_authorized():
            if provenance.intersection(vals) and (
                    self.filtered('softlife_fiscal_invoice_id')
                    or any(vals.get(field) for field in provenance)):
                raise UserError(_('SoftLife fiscal invoice provenance cannot be changed.'))
            if 'state' in vals and self.filtered('softlife_fiscal_invoice_id'):
                raise UserError(_('SoftLife fiscal invoice state is managed internally.'))
            if protected_draft_fields.intersection(vals) and self.filtered(
                    lambda move: move.softlife_fiscal_invoice_id and move.state == 'draft'):
                raise UserError(_('SoftLife fiscal invoice headers are frozen.'))
        payout_provenance = {
            'softlife_payout_bill_id', 'softlife_payout_settlement_id',
            'softlife_payout_tenant_id', 'softlife_payout_period_month',
            'softlife_payout_payload_sha256', 'softlife_payout_source_snapshot_sha256',
            'softlife_payout_bill_snapshot', 'softlife_payout_accounting_key',
            'softlife_payout_recipient_snapshot', 'softlife_payout_recipient_sha256',
        }
        if not _softlife_payout_is_authorized():
            if payout_provenance.intersection(vals) and (
                    self.filtered('softlife_payout_bill_id')
                    or any(vals.get(field) for field in payout_provenance)):
                raise UserError(_('SoftLife payout self-bill provenance cannot be changed.'))
            if 'state' in vals and self.filtered('softlife_payout_bill_id'):
                raise UserError(_('SoftLife payout self-bill state is managed internally.'))
            if protected_draft_fields.union({'name', 'narration'}).intersection(vals) and self.filtered(
                    'softlife_payout_bill_id'):
                raise UserError(_('SoftLife payout self-bill headers are frozen.'))
        return super().write(vals)

    def unlink(self):
        if not _softlife_fiscal_is_authorized() and self.filtered('softlife_fiscal_invoice_id'):
            raise UserError(_('SoftLife fiscal invoices cannot be deleted manually.'))
        if not _softlife_payout_is_authorized() and self.filtered('softlife_payout_bill_id'):
            raise UserError(_('SoftLife payout self-bills cannot be deleted manually.'))
        return super().unlink()

    def action_post(self):
        fiscal_moves = self.filtered('softlife_fiscal_invoice_id')
        if fiscal_moves and not _softlife_fiscal_is_authorized():
            raise UserError(_(
                'SoftLife fiscal invoices can only be posted by validated platform confirmation.'
            ))
        payout_moves = self.filtered('softlife_payout_bill_id')
        if payout_moves and not _softlife_payout_is_authorized():
            raise UserError(_(
                'SoftLife payout self-bills can only be posted by the validated payout handler.'
            ))
        return super().action_post()


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    softlife_fiscal_source_order_id = fields.Char(index=True, copy=False)
    softlife_fiscal_allocation_id = fields.Char(index=True, copy=False)
    softlife_supporting_sale_order_id = fields.Char(index=True, copy=False)
    softlife_supporting_source_sha256 = fields.Char(copy=False)

    _sql_constraints = [
        ('softlife_fiscal_source_order_unique', 'unique(softlife_fiscal_source_order_id)',
         'This SoftLife source transaction has already been invoiced.'),
        ('softlife_fiscal_source_order_id_format',
         "CHECK (softlife_fiscal_source_order_id IS NULL OR softlife_fiscal_source_order_id ~ "
         "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')",
         'The SoftLife fiscal source transaction ID must be a canonical UUID.'),
        ('softlife_fiscal_allocation_unique', 'unique(softlife_fiscal_allocation_id)',
         'This SoftLife fiscal allocation has already been invoiced.'),
        ('softlife_fiscal_allocation_id_format',
         "CHECK (softlife_fiscal_allocation_id IS NULL OR softlife_fiscal_allocation_id ~ "
         "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')",
         'The SoftLife fiscal allocation ID must be a canonical UUID.'),
        ('softlife_supporting_sale_order_id_format',
         "CHECK (softlife_supporting_sale_order_id IS NULL OR softlife_supporting_sale_order_id ~ "
         "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')",
         'The SoftLife supporting sale-order ID must be a canonical UUID.'),
        ('softlife_supporting_sale_order_unique', 'unique(softlife_supporting_sale_order_id)',
         'This SoftLife supporting sales-order document has already been allocated.'),
        ('softlife_supporting_source_sha256_format',
         "CHECK (softlife_supporting_source_sha256 IS NULL OR "
         "softlife_supporting_source_sha256 ~ '^[0-9a-f]{64}$')",
         'The SoftLife supporting sales-order source hash must be lowercase SHA-256.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        provenance = {
            'softlife_fiscal_source_order_id', 'softlife_fiscal_allocation_id',
            'softlife_supporting_sale_order_id', 'softlife_supporting_source_sha256',
        }
        if not _softlife_fiscal_is_authorized():
            if any(provenance.intersection(vals) for vals in vals_list):
                raise UserError(_('SoftLife fiscal source identity is managed internally.'))
            move_ids = {vals.get('move_id') for vals in vals_list if vals.get('move_id')}
            if self.env['account.move'].browse(move_ids).filtered('softlife_fiscal_invoice_id'):
                raise UserError(_('SoftLife fiscal invoice lines are frozen.'))
        if not _softlife_payout_is_authorized():
            move_ids = {vals.get('move_id') for vals in vals_list if vals.get('move_id')}
            if self.env['account.move'].browse(move_ids).filtered('softlife_payout_bill_id'):
                raise UserError(_('SoftLife payout self-bill lines are frozen.'))
        return super().create(vals_list)

    def write(self, vals):
        if not _softlife_fiscal_is_authorized():
            if {
                'softlife_fiscal_source_order_id', 'softlife_fiscal_allocation_id',
                'softlife_supporting_sale_order_id', 'softlife_supporting_source_sha256',
            }.intersection(vals):
                raise UserError(_('SoftLife fiscal source identity cannot be changed.'))
            fiscal_lines = self.filtered(lambda line: line.move_id.softlife_fiscal_invoice_id)
            target_move = self.env['account.move'].browse(vals.get('move_id')).exists()
            if fiscal_lines or target_move.filtered('softlife_fiscal_invoice_id'):
                raise UserError(_('SoftLife fiscal invoice lines are frozen.'))
        if not _softlife_payout_is_authorized():
            payout_lines = self.filtered(lambda line: line.move_id.softlife_payout_bill_id)
            target_move = self.env['account.move'].browse(vals.get('move_id')).exists()
            if payout_lines or target_move.filtered('softlife_payout_bill_id'):
                raise UserError(_('SoftLife payout self-bill lines are frozen.'))
        return super().write(vals)

    def unlink(self):
        if not _softlife_fiscal_is_authorized() and self.filtered(
                lambda line: line.move_id.softlife_fiscal_invoice_id):
            raise UserError(_('SoftLife fiscal invoice lines are frozen.'))
        if not _softlife_payout_is_authorized() and self.filtered(
                lambda line: line.move_id.softlife_payout_bill_id):
            raise UserError(_('SoftLife payout self-bill lines are frozen.'))
        return super().unlink()


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    softlife_payout_bill_id = fields.Char(index=True, copy=False)
    softlife_payout_pdf_sha256 = fields.Char(copy=False)

    _sql_constraints = [
        ('softlife_payout_attachment_bill_unique', 'unique(softlife_payout_bill_id)',
         'A SoftLife payout self-bill can only have one PDF attachment.'),
        ('softlife_payout_attachment_hash_format',
         "CHECK (softlife_payout_pdf_sha256 IS NULL OR "
         "softlife_payout_pdf_sha256 ~ '^[0-9a-f]{64}$')",
         'The SoftLife payout PDF hash must be lowercase SHA-256.'),
        ('softlife_payout_attachment_provenance_paired',
         "CHECK ((softlife_payout_bill_id IS NULL AND softlife_payout_pdf_sha256 IS NULL) OR "
         "(softlife_payout_bill_id IS NOT NULL AND softlife_payout_pdf_sha256 IS NOT NULL))",
         'SoftLife payout PDF provenance must be stored together.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        provenance = {'softlife_payout_bill_id', 'softlife_payout_pdf_sha256'}
        if not _softlife_payout_is_authorized() and any(
                provenance.intersection(vals) for vals in vals_list):
            raise UserError(_('SoftLife payout PDF provenance is managed internally.'))
        return super().create(vals_list)

    def write(self, vals):
        if not _softlife_payout_is_authorized() and {
                'softlife_payout_bill_id', 'softlife_payout_pdf_sha256', 'datas', 'raw',
                'res_model', 'res_id', 'mimetype', 'name',
        }.intersection(vals) and self.filtered('softlife_payout_bill_id'):
            raise UserError(_('SoftLife payout PDF attachments are frozen.'))
        return super().write(vals)

    def unlink(self):
        if not _softlife_payout_is_authorized() and self.filtered('softlife_payout_bill_id'):
            raise UserError(_('SoftLife payout PDF attachments cannot be deleted manually.'))
        return super().unlink()
