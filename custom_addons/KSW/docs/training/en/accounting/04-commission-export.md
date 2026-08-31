# Commissions: Bank Export

**Who is this for:** Accounting (commission accountant)
**How long it takes:** 5–10 minutes per month

## What this does

Once the General Managers have approved the month's commissions and allowances,
you check the **Payment Register**, export the **bank transfer file** and mark the
month **Paid**.

> **This replaced the old commission sheets.** There are no longer per-employee
> commission sheets to *Finalise*, and no commission *batch* to *Close*. The
> Payment Register is generated automatically when the month is approved — one line
> per employee, never typed by hand.

> **Sales & Collection is not in this file.** It is calculated on its own page and
> paid separately.

## Before you start

- The month must be **Approved**. If it is still Open or Submitted, a General
  Manager has not finished — the **Export Bank File** button will not be there.
- Employees are paid to their own salary bank account; anyone without one falls back
  to the run's **Default Paying Bank Account**. Set that on the pay run before
  exporting if you have such employees.

## Steps

1. **Open Commissions → Monthly Pay Run** and pick the approved month.
   ![Approved pay run](../../screenshots/accounting/comm-01.png)

2. **Check the register.** The **Who Gets Paid** tab lists every employee:
   **Earnings**, **Loans Deduction** and **Net Payable** (earnings − loans).
   Open a line's entries to see exactly what produced the earnings.
   ![Payment register](../../screenshots/accounting/comm-02.png)

3. **Correct the loan figure if it is wrong.** You can still adjust an employee's
   **Loans Deduction** on an approved month before the file goes out; the net
   recalculates.

4. **Export Bank File.** Choose the format in the wizard and download it. Lines are
   grouped by the employee's paying bank account.
   ![Export bank file](../../screenshots/accounting/comm-03.png)

5. **Mark Paid** once the transfer has gone out.

## Reporting

**Commissions → Reports → Payment Register** is the same data as a searchable list
across months, groupable by department — use it for reconciliation rather than
opening each run.

## What happens next

The month stays locked. The settled loan installments are already marked paid
against the employees' loans, so nothing has to be recorded twice.

If a genuine correction is needed after approval, only the **company's General
Manager** can **Reopen** the month — that returns the settled installments to
pending and turns the register back into a preview. Ask rather than working around it.

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| No **Export Bank File** button | The month is not approved yet | A GM still has departments to approve — check **By Department** |
| An employee is missing from the register | Their department was never approved, or never handed over | The approval chatter message lists exactly which departments were left out and why |
| An employee has no bank account on the file | No salary bank account on their record | Set the run's **Default Paying Bank Account**, or ask HR to add the employee's |
| The net is not what the supervisor expected | Loan installments were settled out of the commission | Open the line's entries and the employee's loan to see what was taken |
| I need to change an amount | The month is locked | Only the company's General Manager can reopen it |

## Related guides

- Supervisor: [The monthly cycle](../supervisor/06-commission-monthly-cycle.md)
- GM: [Approving the month's commissions](../gm/04-commission-approval.md)
- Payroll: [Bank file export](../payroll/03-bank-file-export.md)
