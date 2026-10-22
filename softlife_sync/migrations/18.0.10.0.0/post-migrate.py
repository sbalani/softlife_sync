def migrate(cr, version):
    cr.execute(
        """
        UPDATE sale_order
           SET softlife_sale_key = 'legacy:' || softlife_export_id || ':'
               || softlife_warehouse_id::text
         WHERE softlife_export_id IS NOT NULL
           AND softlife_warehouse_id IS NOT NULL
           AND softlife_sale_key IS NULL
        """
    )
    cr.execute(
        """
        UPDATE sale_order sale
           SET softlife_manufacturing_contract_version = COALESCE(
                   sale.softlife_manufacturing_contract_version, 2),
               softlife_document_date = COALESCE(
                   sale.softlife_document_date, run.document_date, sale.date_order::date)
          FROM softlife_manufacturing_run run
         WHERE sale.softlife_export_id = run.export_id
           AND sale.softlife_export_id IS NOT NULL
           AND (sale.softlife_manufacturing_contract_version IS NULL
                OR sale.softlife_document_date IS NULL)
        """
    )
    cr.execute(
        """
        UPDATE sale_order
           SET softlife_manufacturing_contract_version = COALESCE(
                   softlife_manufacturing_contract_version, 2),
               softlife_document_date = COALESCE(
                   softlife_document_date, date_order::date)
         WHERE softlife_export_id IS NOT NULL
           AND (softlife_manufacturing_contract_version IS NULL
                OR softlife_document_date IS NULL)
        """
    )
