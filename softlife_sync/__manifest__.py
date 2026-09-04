{
    'name': 'SoftLife Platform Sync',
    'version': '18.0.4.0.0',
    'summary': 'Synchronize SoftLife master data and manufacturing periods with Odoo.',
    'description': """
SoftLife Platform Sync
======================
Downstream ERP connector: reads the SoftLife platform (Supabase REST, the
system of record operated by the middleware) and mirrors into Odoo:

  tenants        -> res.partner (customers)
  products       -> product.template
  manufacturing periods -> MOs, warehouse sales orders and validated deliveries

...and mirrors Odoo's own SKU/lot/warehouse master data back out to Supabase
(read-only mirror tables the platform consumes — see softlife-platform/README):

  product.product   -> odoo_products
  stock.lot          -> odoo_lots
  stock.warehouse     -> odoo_warehouses
  stock.quant         -> odoo_lot_stock (lot quantity per warehouse)

Linking a platform ingredient to an Odoo SKU (products.odoo_id) is never done
by this module automatically — it's a deliberate choice made on the platform
(see /odoo and /products there). Records removed/archived in Odoo are pruned
from the mirror tables on the next sync; any ingredient linked to a pruned
row is automatically unlinked (FK is ON DELETE SET NULL), never re-pointed.

Odoo no longer talks to Huaxin directly — the middleware owns Huaxin.
Idempotent by immutable platform ids with SQL uniqueness in Odoo. Run via
Settings, the Platform Sync / Manufacturing menus, or scheduled crons.
""",
    'author': 'SoftLife',
    'website': 'https://softlife.es',
    'category': 'Accounting/Accounting',
    'license': 'OPL-1',
    'depends': [
        'softlife_machine', 'account', 'stock', 'mrp', 'sale_management',
        'product_secondary_unit', 'stock_secondary_unit',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/softlife_sync_data.xml',
        'data/package_uom_precision.xml',
        'data/ir_cron.xml',
        'views/res_config_settings_views.xml',
        'views/product_template_views.xml',
        'views/manufacturing_run_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': False,
}
