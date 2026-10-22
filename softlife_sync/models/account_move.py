from contextlib import contextmanager
from contextvars import ContextVar

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_FISCAL_AUTHORITY = object()
_fiscal_authority = ContextVar('softlife_fiscal_authority', default=None)


@contextmanager
def _softlife_fiscal_internal_scope():
    token = _fiscal_authority.set(_FISCAL_AUTHORITY)
    try:
        yield
    finally:
        _fiscal_authority.reset(token)


def _softlife_fiscal_is_authorized():
    return _fiscal_authority.get() is _FISCAL_AUTHORITY


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

    @api.model_create_multi
    def create(self, vals_list):
        provenance = {
            'softlife_fiscal_invoice_id', 'softlife_fiscal_payload_sha256',
            'softlife_fiscal_invoice_snapshot', 'softlife_fiscal_accounting_key',
        }
        if not _softlife_fiscal_is_authorized() and any(
                provenance.intersection(vals) for vals in vals_list):
            raise UserError(_('SoftLife fiscal invoice provenance is managed internally.'))
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
        return super().write(vals)

    def unlink(self):
        if not _softlife_fiscal_is_authorized() and self.filtered('softlife_fiscal_invoice_id'):
            raise UserError(_('SoftLife fiscal invoices cannot be deleted manually.'))
        return super().unlink()

    def action_post(self):
        fiscal_moves = self.filtered('softlife_fiscal_invoice_id')
        if fiscal_moves and not _softlife_fiscal_is_authorized():
            raise UserError(_(
                'SoftLife fiscal invoices can only be posted by validated platform confirmation.'
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
        return super().write(vals)

    def unlink(self):
        if not _softlife_fiscal_is_authorized() and self.filtered(
                lambda line: line.move_id.softlife_fiscal_invoice_id):
            raise UserError(_('SoftLife fiscal invoice lines are frozen.'))
        return super().unlink()
