from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    _softlife_secondary_code = 'SOFTLIFE_CONTENT'

    supabase_id = fields.Char(string='SoftLife ID', index=True, copy=False)
    package_content_quantity = fields.Float(
        string='Net Content per Unit',
        digits=(16, 6),
        help='Physical product content in one inventory unit/package, for example 1120 g.',
    )
    package_content_uom = fields.Selection(
        [('g', 'g'), ('kg', 'kg'), ('ml', 'ml'), ('L', 'L')],
        string='Content UoM',
        help='Physical unit used by Net Content per Unit. This does not change the inventory UoM.',
    )

    @api.constrains('package_content_quantity', 'package_content_uom')
    def _check_package_content(self):
        for product in self:
            has_quantity = bool(product.package_content_quantity)
            has_uom = bool(product.package_content_uom)
            if has_quantity != has_uom:
                raise ValidationError(_(
                    'Net Content per Unit and Content UoM must either both be set or both be empty.'
                ))
            if product.package_content_quantity < 0:
                raise ValidationError(_('Net Content per Unit must be positive.'))

    def _softlife_content_uom(self):
        self.ensure_one()
        xmlids = {
            'g': ('uom.product_uom_gram',),
            'kg': ('uom.product_uom_kgm',),
            'ml': ('uom.product_uom_ml', 'uom.product_uom_milliliter'),
            'L': ('uom.product_uom_litre',),
        }
        for xmlid in xmlids.get(self.package_content_uom, ()):
            uom = self.env.ref(xmlid, raise_if_not_found=False)
            if uom:
                return uom
        raise ValidationError(_(
            'Odoo UoM %s is not installed; the package content cannot be configured.'
        ) % self.package_content_uom)

    def _sync_softlife_secondary_uom(self):
        Secondary = self.env['product.secondary.unit'].with_context(active_test=False)
        for product in self:
            secondary = Secondary.search([
                ('product_tmpl_id', '=', product.id),
                ('code', '=', self._softlife_secondary_code),
            ], limit=1)
            if not product.package_content_quantity or not product.package_content_uom:
                if secondary:
                    secondary.active = False
                continue
            values = {
                'name': _('Net Content (%s)') % product.package_content_uom,
                'code': self._softlife_secondary_code,
                'product_tmpl_id': product.id,
                'uom_id': product._softlife_content_uom().id,
                'dependency_type': 'dependent',
                'factor': 1.0 / product.package_content_quantity,
                'active': True,
            }
            secondary.write(values) if secondary else Secondary.create(values)

    @api.model_create_multi
    def create(self, values_list):
        products = super().create(values_list)
        products._sync_softlife_secondary_uom()
        return products

    def write(self, values):
        result = super().write(values)
        if {'package_content_quantity', 'package_content_uom'} & set(values):
            self._sync_softlife_secondary_uom()
        return result


class ProductProduct(models.Model):
    _inherit = 'product.product'

    softlife_recipe_id = fields.Char(string='SoftLife Recipe ID', index=True, copy=False)

    _sql_constraints = [
        ('softlife_recipe_id_unique', 'unique(softlife_recipe_id)',
         'A SoftLife recipe can only have one finished product.'),
    ]
