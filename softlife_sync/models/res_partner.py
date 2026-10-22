from contextlib import contextmanager
from contextvars import ContextVar

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_FISCAL_PARTNER_AUTHORITY = object()
_fiscal_partner_authority = ContextVar('softlife_fiscal_partner_authority', default=None)


@contextmanager
def _softlife_fiscal_partner_internal_scope():
    token = _fiscal_partner_authority.set(_FISCAL_PARTNER_AUTHORITY)
    try:
        yield
    finally:
        _fiscal_partner_authority.reset(token)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    supabase_id = fields.Char(string='SoftLife ID', index=True, copy=False)
    softlife_fiscal_customer_id = fields.Char(index=True, copy=False)
    softlife_fiscal_customer_sha256 = fields.Char(copy=False)

    _sql_constraints = [
        ('softlife_fiscal_customer_id_unique', 'unique(softlife_fiscal_customer_id)',
         'A SoftLife fiscal customer can only be linked to one Odoo partner.'),
        ('softlife_fiscal_customer_provenance_paired',
         "CHECK ((softlife_fiscal_customer_id IS NULL AND softlife_fiscal_customer_sha256 IS NULL) OR "
         "(softlife_fiscal_customer_id IS NOT NULL AND softlife_fiscal_customer_sha256 IS NOT NULL))",
         'SoftLife fiscal customer identity and snapshot hash must be stored together.'),
        ('softlife_fiscal_customer_id_format',
         "CHECK (softlife_fiscal_customer_id IS NULL OR softlife_fiscal_customer_id ~ "
         "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')",
         'The SoftLife fiscal customer ID must be a canonical UUID.'),
        ('softlife_fiscal_customer_sha256_format',
         "CHECK (softlife_fiscal_customer_sha256 IS NULL OR softlife_fiscal_customer_sha256 ~ "
         "'^[0-9a-f]{64}$')",
         'The SoftLife fiscal customer snapshot hash must be lowercase SHA-256.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        provenance = {'softlife_fiscal_customer_id', 'softlife_fiscal_customer_sha256'}
        if _fiscal_partner_authority.get() is not _FISCAL_PARTNER_AUTHORITY and any(
                provenance.intersection(vals) for vals in vals_list):
            raise UserError(_('SoftLife fiscal customer provenance is managed internally.'))
        return super().create(vals_list)

    def write(self, vals):
        provenance = {'softlife_fiscal_customer_id', 'softlife_fiscal_customer_sha256'}
        snapshot_fields = {
            'name', 'vat', 'email', 'street', 'city', 'zip', 'state_id', 'country_id',
            'company_id', 'softlife_fiscal_customer_id', 'softlife_fiscal_customer_sha256',
        }
        if _fiscal_partner_authority.get() is not _FISCAL_PARTNER_AUTHORITY:
            if provenance.intersection(vals):
                raise UserError(_('SoftLife fiscal customer provenance cannot be changed.'))
            if snapshot_fields.intersection(vals) and self.filtered('softlife_fiscal_customer_id'):
                raise UserError(_('SoftLife fiscal customer snapshot fields are frozen.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered('softlife_fiscal_customer_id'):
            raise UserError(_('SoftLife fiscal customers cannot be deleted.'))
        return super().unlink()
