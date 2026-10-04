def migrate(cr, version):
    # 19.0.1.1.0 stored BAS depreciation as fixed daily rows; BAS's figure
    # depends on where the range starts, so the rows were replaced by
    # ksw.bas.fixed.asset (computed per range). Drop the orphan table.
    cr.execute('DROP TABLE IF EXISTS ksw_bas_depreciation_day')
