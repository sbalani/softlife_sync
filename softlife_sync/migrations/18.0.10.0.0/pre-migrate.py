def migrate(cr, version):
    cr.execute(
        'ALTER TABLE sale_order DROP CONSTRAINT IF EXISTS '
        'sale_order_softlife_sale_order_unique'
    )
    cr.execute(
        'ALTER TABLE sale_order DROP CONSTRAINT IF EXISTS '
        'sale_order_softlife_source_identity_paired'
    )
