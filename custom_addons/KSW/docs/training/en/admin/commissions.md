# Commissions Configuration & the Officer's View

**Who is this for:** Commission Admin (configuration) and Commission Officer (company-wide view)
**How long it takes:** varies

## What this does

Everything KSW pays on top of salary is a **configuration record**, not a
programmed feature. Adding a new kind of pay — a new allowance, a new bonus — is a
row in the **Pay Components** catalog: no development, no new menu, no new access
group. This page covers that catalog, the work sites behind it, and the
company-wide screens the Commission Officer works from.

> **Sales & Collection has its own configuration** (rules, salesperson profiles)
> and is paid separately from the commission run. It is not a pay component and is
> not covered here.

## Pay Components — the catalog

**Commissions → Configuration → Pay Components.**

Everything reduces to **quantity × rate → amount**. Four calculation methods cover
every case:

| Calculation | What it does | Used by |
|---|---|---|
| **Fixed amount** | Nothing is derived — the supervisor types the amount | Allowances, holiday bonuses, Other |
| **Quantity × rate** | The rate configured on the component (or on the chosen option) | Meals, Friday Work |
| **Quantity × salary-derived rate** | basic salary ÷ **divisor** × **factor** × quantity | Overtime (divisor 240, factor 1.5 — Labour Law art. 107) |
| **Tiered on quantity** | A waterfall through the rate tiers, above a per-entry free allowance | Driver Trips |

![Pay component form](../../screenshots/admin/comp-01.png)

### Options — one component, several rates

If the *only* difference between two kinds of pay is the unit rate, they are
**options** of one component, not two components. Meals is one component with
Breakfast (10), Lunch (20) and Dinner (15) inside it — so a supervisor records the
month's meals on one screen and picks the meal on each row, instead of opening,
submitting and having three batches approved.

Anything that genuinely differs — a different scope, a different calculation, a
different person allowed to record it — stays a component of its own.

> Options only work with **Quantity × rate**; each option carries its own rate.

### Rate tiers — for tiered components

Each tier has a **Band Size** and a **Rate**, consumed in sequence.
**Leave the Band Size at 0 on the last tier** so it absorbs everything above the
previous bands. Add a **Work Site** to a tier to give that site its own ladder; a
site with no tiers of its own uses the ones with no site set.

### What an entry looks like

| Setting | Effect on the entry screen |
|---|---|
| **Quantity Label** | What the quantity column is called — Hours, Trips, Meals, Days |
| **Reference Quantity Label** | Shows a second, informational column (the driver's raw trip count). Leave empty to hide it |
| **Per Occurrence** | Each row is a dated occurrence and the date is required |
| **Ask for Location** | Shows and asks for the location |
| **Ask for a Reason** | A short justification is required on every row |
| **Scope** | What a batch covers: **Department**, **Work Site** or **Company-wide** |
| **Restricted To** | Leave empty for any commission supervisor; set it to limit the component to particular roles |
| **Import Source** | Adds an **Import** button on batches of this component (Driver trips from BAS) |

### Adding a new kind of pay

1. **New**, give it a name and a unique **Code** (the code is what exports and
   reports key on — pick it once and leave it alone).
2. Choose **Earning** or **Deduction**, the **calculation**, and the **scope**.
3. Fill in the rate / divisor and factor / tiers the calculation needs.
4. Set the entry-shape flags above so the supervisor is asked for exactly what the
   pay needs — no more.
5. Tell the supervisors it exists.

Existing components are never overwritten by an upgrade, so rates you edit here
survive.

## Who may record what — the opt-in entry groups

The **Supervisor** tier is the *scope* (which departments). The per-kind entry
groups — **Overtime Entry**, **Driver Commission Entry**, **Location Allowance
Entry** — are the *opt-in*, and they are **not** implied by the Supervisor tier.

> **Every supervisor must be ticked for the kinds of pay he handles**, on his user
> record, or he will correctly see nothing. This is the most common "the app is
> broken" report after a new supervisor is set up.

The Officer tier implies all of them, so Officers, Accountants and GMs keep seeing
everything.

## Work Sites

**Commissions → Configuration → Work Sites.** A site is the scope of a
site-scoped component (Driver Trips) and the key the BAS trip import matches on.
Site-specific rate tiers live on the component, pointing at the site.

## Recurring entries

Standing monthly instructions (a mobile-phone allowance, a project-management
allowance) are now maintained by **supervisors for their own teams**, under
**Commissions → Pay Entries → Recurring Entries**. The Officer keeps the
company-wide view of the same list and can correct any row.

See the supervisor guide, [Recurring entries](../supervisor/05-commission-recurring.md),
for what they are and when to create one.

## The Commission Officer's company-wide screens

| Screen | What it is for |
|---|---|
| **Pay Entries → All Entries** | Every entry company-wide, grouped by employee. Search and reconciliation |
| **Reports → Payment Register** | One line per employee per month: earnings, loan recovery, net |
| **Monthly Pay Run → Close the Month** | Marks the month as handed over when a department never submitted. It does **not** lock the period and it does **not** pay anything — that is the GM's approval |

**Locking and reopening a month belong to the company's General Manager**, not to
the Officer: reopening unwinds every department's loan settlement at once.

## Deployment prerequisite

Supervisor scope keys on **`hr.department` → Manager**. A department with no
manager set gives its supervisor nothing to record, with a clear message telling him
to ask HR. Check every department has a manager before rolling the app out to a new
team. (This is the **direct manager**, a different person from the department's
**General Manager**, which is its own field and drives approval.)

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| A supervisor sees no components | He is not ticked for any entry type | Tick the entry groups on his user record |
| A supervisor sees nothing to record at all | His department has no Manager | Set `hr.department` → Manager, or add him as an assistant to it |
| "…has options, so its amount has to be a quantity × rate" | Options were added to a fixed or tiered component | Options only apply to Quantity × rate |
| "…is tiered but has no rate tiers configured" | A tiered component was saved with an empty ladder | Add the tiers, last one with Band Size 0 |
| A tiered amount stops short | The last tier has a Band Size | Set the last tier's Band Size to 0 |
| A new component does not appear for anyone | **Restricted To** was filled in | Clear it, or add the supervisors' groups |

## Related guides

- Supervisor: [Recording extra pay](../supervisor/04-commission-pay-entries.md)
- GM: [Approving the month's commissions](../gm/04-commission-approval.md)
- Accounting: [Commissions: bank export](../accounting/04-commission-export.md)
- [Deductions configuration](deductions.md)
