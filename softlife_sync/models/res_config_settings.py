from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    softlife_supabase_url = fields.Char(
        string='Supabase URL', config_parameter='softlife.sync.supabase_url',
    )
    softlife_supabase_key = fields.Char(
        string='Service Role Key', config_parameter='softlife.sync.supabase_key',
    )
    softlife_default_partner_id = fields.Many2one(
        'res.partner', string='Default customer for orders',
        config_parameter='softlife.sync.default_partner_id',
        help='Customer set on invoices created from vending orders.',
    )
    softlife_default_product_id = fields.Many2one(
        'product.product', string='Default product for order lines',
        config_parameter='softlife.sync.default_product_id',
        help='Product used on the invoice line (provides the revenue account).',
    )
    softlife_last_sync = fields.Char(string='Last sync', compute='_compute_softlife_last_sync')
    softlife_last_sync_summary = fields.Char(string='Last result', compute='_compute_softlife_last_sync')

    @api.depends('softlife_supabase_url')
    def _compute_softlife_last_sync(self):
        icp = self.env['ir.config_parameter'].sudo()
        last = icp.get_param('softlife.sync.last_sync')
        summary = icp.get_param('softlife.sync.last_sync_summary')
        for rec in self:
            rec.softlife_last_sync = last or ''
            rec.softlife_last_sync_summary = summary or ''

    def action_sync_now(self):
        msg = self.env['softlife.sync.client'].sudo().sync_all()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': 'SoftLife Sync', 'message': msg, 'type': 'success', 'sticky': False},
        }
