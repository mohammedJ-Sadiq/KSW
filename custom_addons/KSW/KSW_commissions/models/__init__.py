from . import hr_department
from . import ksw_commission_lock         # period lock predicate + guard (no models)
from . import ksw_site                    # work sites (used by entries and tiers)
from . import ksw_pay_component           # the catalog: a pay type is data, not code
from . import hr_employee
from . import ksw_pay_batch               # batches + entries: the supervisor's screen
from . import ksw_pay_recurring           # SAP infotype 0014 equivalent
from . import ksw_pay_submission          # one department's handover to the GM
from . import ksw_pay_run                 # the month, its approval and the register
from . import ksw_pay_import_bas          # the one importer: driver trips from BAS
from . import ksw_meal_settings           # res.config.settings: overtime params
from . import ksw_salesperson_profile     # sales: yearly target + client splits
from . import ksw_sales_commission_rule   # sales: rule + tier catalog
from . import ksw_sales_commission_sheet  # sales: paid separately, not an entry type
from . import ksw_sales_commission_addition_line   # accountant manual bonus/reward lines
from . import ksw_sales_commission_deduction_line  # accountant manual penalty/correction lines
from . import res_partner                 # commission import name alias
from . import ksw_bas_customer            # BAS-native rep resolution + AR aging target
from . import ksw_deduction               # adds awaiting-commission helpers
from . import ksw_deduction_line          # parked installments + pay-run settlement link
from . import hr_payslip                  # filters parked KSW_DED_* inputs out of payslips
