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
| I manage a team / approve my team's requests, and record their overtime, allowances & commissions | **Supervisor** → [`en/supervisor/`](en/supervisor/) |
| I work in HR — approve leave/loans at the HR step, create advances & penalties | **HR** → [`en/hr/`](en/hr/) |
| I work in Accounting — approve loans, create penalties, export the commission bank file | **Accounting** → [`en/accounting/`](en/accounting/) |
| I am the General Manager — initial & final approvals, return a request for revision, approve my departments' monthly commissions | **GM** → [`en/gm/`](en/gm/) |
| I run the monthly payroll — generate payslip batches, export the bank file | **Payroll** → [`en/payroll/`](en/payroll/) |
| I manage deductions across the company / configure deduction types | **Deduction Officer** → [`en/admin/deductions.md`](en/admin/) |
| I configure commissions — pay components, work sites, rules, salespeople; or I am the Commission Officer | **Commission Admin / Officer** → [`en/admin/commissions.md`](en/admin/) |
| I am on the IT team — I handle support tickets and the IT asset register | **IT Team** → [`en/it/`](en/it/) |

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
| `pdf/KSW-Supervisor-Guide-EN.pdf` | DM approvals, attendance sheets, pay entries & recurring entries, the monthly commission cycle, team payslips |
| `pdf/KSW-HR-Guide-EN.pdf` | Leave/loan HR steps, advances, installment changes, attendance review, bank export |
| `pdf/KSW-Accounting-Guide-EN.pdf` | Leave/loan Accounting steps, disbursement, penalties, commission bank export |
| `pdf/KSW-GM-Guide-EN.pdf` | Initial/final approvals, return-to-approver, monthly commission approval |
| `pdf/KSW-Payroll-Guide-EN.pdf` | Generate batch, skipped employees, bank file export |
| `pdf/KSW-Admin-Guide-EN.pdf` | Deduction & commission configuration |
| `pdf/KSW-IT-Guide-EN.pdf` | Ticket queue, resolving & closing, IT asset register, reporting, helpdesk configuration |

### Commission-only handbooks → [`pdf/commission/`](pdf/commission/)

The commission workflow spans four roles, and each role's own handbook mixes it
in with time off, attendance and tickets. `pdf/commission/` carries the
commission content **on its own** — what you hand to somebody being trained on
the commission app and nothing else. **Sales & Collection is excluded**
throughout (own page, own roles, paid separately).

| Handbook | Covers |
|---|---|
| `KSW-Commission-Supervisor-{EN,AR}.pdf` | Pay entries and each component type, recurring entries, the monthly cycle |
| `KSW-Commission-GM-{EN,AR}.pdf` | Approving your departments, returning with a reason, who locks the month |
| `KSW-Commission-Accounting-{EN,AR}.pdf` | Checking the register, bank export, mark paid |
| `KSW-Commission-Configuration-{EN,AR}.pdf` | Pay components, options, tiers, entry groups; the Officer's company-wide screens |
| `KSW-Commission-All-Roles-{EN,AR}.pdf` | All four, in the order the work happens — the end-to-end picture |

```bash
cd tools
node build_pdf.mjs commission        # both languages
node build_pdf.mjs commission en     # English only
```

Rebuilding this bundle does **not** touch the per-role handbooks, and vice
versa — the commission pages live in the role folders and are simply gathered
here as well.

### Standalone single-topic handouts

Some topics are also built as their own small PDF, for handing out on their
own without the whole role handbook:

| Handout | Covers |
|---|---|
| `pdf/KSW-Return-Confirmation-Guide-{EN,AR}.pdf` | Confirming an employee's return from annual leave |
| `pdf/KSW-Raise-Support-Ticket-Guide-{EN,AR}.pdf` | Raising an IT support ticket (any employee) |
| `pdf/KSW-Ticket-For-Team-Member-Guide-{EN,AR}.pdf` | Raising an IT ticket for a direct report (managers) |

```bash
node build_pdf.mjs single <relPath> <slug> "<Title EN>" "<Title AR>"
# e.g. node build_pdf.mjs single employee/05-raise-support-ticket.md \
#        Raise-Support-Ticket "Raising an IT Support Ticket" "رفع تذكرة دعم فني"
```

Rebuild the role handbooks any time (picks up guide edits and new screenshots):

```bash
cd tools
node build_pdf.mjs            # per-role PDFs, both languages
node build_pdf.mjs en         # per-role PDFs, English only
node build_pdf.mjs full       # one comprehensive all-roles manual instead
```
