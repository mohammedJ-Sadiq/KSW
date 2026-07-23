# KSW System — User Training Manual

This folder is the permanent, self-service "how do I…?" reference for everyone
who uses the KSW HR & Payroll system. It is written **by role**: find your role
below and read only your section.

Guides exist in two languages, mirrored file-for-file:

- **English** → [`en/`](en/)
- **العربية** → [`ar/`](ar/) *(RTL, Saudi functional register)*

Screenshots are shared by both languages and live in [`screenshots/`](screenshots/).

---

## Which guide do I read? (role → persona)

You may hold more than one role. Read every persona that matches what you do.

| Your job / what you do | Read this persona guide |
|---|---|
| Any staff member — see my attendance, request leave, request a loan, view my payslip | **Employee** → [`en/employee/`](en/employee/) |
| I manage a team / approve my team's requests (first approval step) | **Supervisor** → [`en/supervisor/`](en/supervisor/) |
| I work in HR — approve leave/loans at the HR step, create advances & penalties | **HR** → [`en/hr/`](en/hr/) |
| I work in Accounting — approve loans, record loan payments, finalise commissions | **Accounting** → [`en/accounting/`](en/accounting/) |
| I am the General Manager — initial & final approvals, return a request for revision | **GM** → [`en/gm/`](en/gm/) |
| I run the monthly payroll — generate payslip batches, export the bank file | **Payroll** → [`en/payroll/`](en/payroll/) |
| I manage deductions across the company / configure deduction types | **Deduction Officer** → [`en/admin/deductions.md`](en/admin/) |
| I configure commissions — categories, sites, rules, salespeople | **Commission Admin** → [`en/admin/commissions.md`](en/admin/) |

> **New to the system? Start here regardless of role:**
> [Getting Started](en/00-getting-started.md) — login, navigation, notifications,
> and the "Waiting For Me" filter every approver uses.

---

## How this manual is maintained

- **Source of truth:** each guide describes the live system. When a workflow or
  button changes, update the matching guide (and its Arabic mirror).
- **Screenshots:** captured from the **development** system only
  (`KSW_dev.conf` / `odoo_dev` / `localhost:8070`) — never production. To
  regenerate them, see [`tools/`](tools/).
- **New guide?** copy [`_template.md`](_template.md) so every page has the same
  shape (Purpose → Who can do this → Steps → What happens next → Common issues).
- **Arabic terminology** follows the approved glossary at
  `custom_addons/KSW/ar_glossary_review.csv`. Use the `proposed_ar` term; never
  translate literally.

## Printable PDF handbooks

Ready-to-hand-out PDFs live in [`pdf/`](pdf/) — **one focused handbook per
role** (only that role's tasks), plus a shared **General** handbook with the
basics everyone needs. Give each person their role handbook + the General one.

| Handbook | Covers |
|---|---|
| `pdf/KSW-General-Guide-EN.pdf` | **Everyone** — login, navigation, notifications, switching to Arabic |
| `pdf/KSW-Employee-Guide-EN.pdf` | Attendance, time off, loans, payslip, commission (self-service) |
| `pdf/KSW-Supervisor-Guide-EN.pdf` | DM approvals, attendance & commission sheets, team payslips |
| `pdf/KSW-HR-Guide-EN.pdf` | Leave/loan HR steps, advances, installment changes, attendance review, bank export |
| `pdf/KSW-Accounting-Guide-EN.pdf` | Leave/loan Accounting steps, disbursement, penalties, commission finalise |
| `pdf/KSW-GM-Guide-EN.pdf` | Initial/final approvals, return-to-approver |
| `pdf/KSW-Payroll-Guide-EN.pdf` | Generate batch, skipped employees, bank file export |
| `pdf/KSW-Admin-Guide-EN.pdf` | Deduction & commission configuration |

Rebuild them any time (picks up guide edits and new screenshots):

```bash
cd tools
node build_pdf.mjs            # per-role PDFs, both languages
node build_pdf.mjs en         # per-role PDFs, English only
node build_pdf.mjs full       # one comprehensive all-roles manual instead
```
