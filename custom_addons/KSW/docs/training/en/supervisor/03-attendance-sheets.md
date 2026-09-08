# Attendance Sheets (Non-Biometric Staff)

**Who is this for:** Supervisor / Direct Manager
**How long it takes:** 5 minutes per month

## What this does

Lets you record the monthly attendance of team members who are **not** on the
fingerprint/face device. You mark each workday present or absent; the sheet then
feeds payroll (uncovered absences cause deductions).

## Before you start

- This is only for **non-biometric** employees who report to you.
- You edit **and confirm** a sheet during its month; once the month closes it
  becomes read-only and only HR can act on it.
- Days already covered by an **approved leave** are **locked** — you can't (and
  shouldn't) change them.

## Steps

1. **Open Attendance → Attendance Sheets.**
   ![Attendance sheets list](../../screenshots/supervisor/attsheet-01.png)

2. **Generate the sheets** for the month if they don't exist yet — use
   **Generate All Sheets** on the list.

3. **Open an employee's sheet.** In the **Daily Attendance** tab you'll see one
   row per day.

4. **Mark attendance:**
   - Toggle each day between present (green) and absent (red), **or**
   - Use **All Present** / **All Absent** to set the whole month, then fix the
     exceptions.
   ![Marking daily attendance](../../screenshots/supervisor/attsheet-02.png)

5. Days with a linked **approved leave** are already filled and locked — leave
   them as they are. Friday/Saturday rest days are calculated by the system and
   cannot be toggled.

6. **Press ✓ Confirm & Send to Payroll** before the month ends. Nothing reaches
   payroll until you do — see [What changed on the attendance sheet](../whatsnew/attendance-sheet-changes.md).

## What happens next

Once you confirm it, the sheet is locked and payroll reads it. **A month you do
not confirm is paid as zero attendance** — it is never confirmed for you. If a
leave is approved afterwards for a day you had confirmed, the confirmation is
withdrawn and you are asked to confirm again. If a confirmed sheet needs
reopening, an **HR / attendance manager** can Reset it to Draft (see the HR
guide).

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| A day won't toggle | It's covered by an approved leave (locked) | That's expected — don't change it |
| No sheet for an employee | Not generated yet | Use **Generate All Sheets** |
| The sheet is read-only | The month has closed | Ask HR to Reset to Draft if a correction is genuinely needed |
| The employee was paid zero days | The month was never confirmed | Confirm it — after the month closes only HR can |

## Related guides

- [What changed on the attendance sheet](../whatsnew/attendance-sheet-changes.md)
- [Approve time off](01-approve-time-off.md)
- [Recording extra pay](04-commission-pay-entries.md)
