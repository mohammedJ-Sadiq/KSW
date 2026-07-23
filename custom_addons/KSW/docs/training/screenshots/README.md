# Screenshots

Shared image pool for the training manual. **Both** the English (`en/`) and
Arabic (`ar/`) guides reference the same files here, so a screenshot is only
captured once.

## Naming convention

```
screenshots/<persona>/<workflow>-NN.png
```

- `<persona>` — `common`, `employee`, `supervisor`, `hr`, `accounting`, `gm`,
  `payroll`, `admin`.
- `<workflow>` — short slug matching the guide (e.g. `loan`, `timeoff`,
  `leave-hr`).
- `NN` — two-digit step number (`01`, `02`, …).

Example: `screenshots/employee/loan-03.png` is step 3 of the employee
"Request a loan" guide.

## How they are captured

Screenshots are generated from the **development** system only
(`KSW_dev.conf` / `odoo_dev` / `localhost:8070`) — never production — using the
automation in [`../tools/`](../tools/). See that folder for how to (re)generate
them after a UI change.

Until the pipeline runs, guides reference filenames that may not exist yet; the
written content is complete regardless.
