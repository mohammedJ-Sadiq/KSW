#!/usr/bin/env python3
"""Apply Arabic translation corrections from ar_corrections.csv.

Workflow:
  1. Arabic users edit  custom_addons/KSW/ar_corrections.csv
     (module, english, arabic).
  2. Admin runs:
       python3 custom_addons/KSW/_apply_corrections.py
  3. The script:
       a) Updates the matching ar_001.po file(s) on disk.
       b) Imports the changed files into the odoo_dev DB (-w overwrite).
  4. Changes take effect immediately — no service restart needed.

Usage:
    python3 _apply_corrections.py                    # apply all corrections
    python3 _apply_corrections.py --dry-run          # preview only, no writes
    python3 _apply_corrections.py --module KSW_payroll  # one module only
"""

import argparse
import csv
import os
import subprocess
import sys

BASE     = '/home/odoo/Odoo/odoo/custom_addons/KSW'
CSV_FILE = os.path.join(BASE, 'ar_corrections.csv')
ODOO_BIN = '/home/odoo/odoo19env/bin/python3.12'
ODOO     = '/home/odoo/Odoo/odoo/odoo-bin'
CONF     = '/home/odoo/Odoo/odoo/KSW_dev.conf'
DB       = 'odoo_dev'

# All KSW module names — used when module == "any"
ALL_MODULES = [
    'KSW_annual_leave', 'KSW_attendance_leave', 'KSW_attendance_report',
    'KSW_attendance_sheet', 'KSW_base_security', 'KSW_commissions',
    'KSW_deduction', 'KSW_eos_leave', 'KSW_ext_sync', 'KSW_leave_approval',
    'KSW_leave_types', 'KSW_payroll', 'KSW_unpaid_leave',
    'KSW_working_schedule',
]


def load_corrections(module_filter=None):
    """Read ar_corrections.csv, return list of (modules, english, arabic)."""
    corrections = []
    with open(CSV_FILE, newline='', encoding='utf-8') as fh:
        reader = csv.reader(fh)
        for lineno, row in enumerate(reader, 1):
            # Skip blank lines and comment lines
            if not row or row[0].lstrip().startswith('#'):
                continue
            if len(row) < 3:
                print(f'  [warn] line {lineno}: expected 3 columns, got {len(row)} — skipped')
                continue
            mod_raw, english, arabic = row[0].strip(), row[1].strip(), row[2].strip()
            if not english or not arabic:
                print(f'  [warn] line {lineno}: empty english or arabic — skipped')
                continue
            # Expand "any"
            if mod_raw.lower() == 'any':
                modules = ALL_MODULES
            else:
                modules = [mod_raw]
            # Apply module filter
            if module_filter:
                modules = [m for m in modules if m == module_filter]
            if modules:
                corrections.append((modules, english, arabic))
    return corrections


def apply_to_po(po_path, english, arabic, dry_run):
    """Update one ar_001.po file. Returns True if a change was made."""
    try:
        import polib
    except ImportError:
        sys.exit('polib not installed — run: pip install polib')

    if not os.path.exists(po_path):
        return False

    po = polib.pofile(po_path)
    changed = False
    for entry in po:
        if entry.obsolete or entry.msgid_plural:
            continue
        if entry.msgid == english:
            if entry.msgstr != arabic:
                if not dry_run:
                    entry.msgstr = arabic
                changed = True
            break
    if changed and not dry_run:
        po.save(po_path)
    return changed


def import_po(po_path, dry_run):
    """Import a single ar_001.po file into the DB using odoo-bin i18n import."""
    if dry_run:
        print(f'    [dry-run] would import: {po_path}')
        return
    cmd = [
        ODOO_BIN, ODOO,
        'i18n', 'import', po_path,
        '-l', 'ar_001',
        '-w',
        '-c', CONF,
        '-d', DB,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # "translations are loaded successfully" appears in stderr
    if 'translations are loaded successfully' in result.stderr:
        print(f'    ✓ imported into DB')
    else:
        # Print last few lines for diagnosis
        tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        print(f'    [warn] import may have failed:\n    ' + '\n    '.join(tail))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without writing anything')
    parser.add_argument('--module', metavar='KSW_xxx',
                        help='Limit to one module')
    args = parser.parse_args()

    corrections = load_corrections(module_filter=args.module)
    if not corrections:
        print('No corrections found in ar_corrections.csv.')
        return

    print(f'{"[DRY RUN] " if args.dry_run else ""}Applying {len(corrections)} correction(s)...\n')

    # Track which modules were actually changed so we only import those
    dirty_modules = set()

    for modules, english, arabic in corrections:
        for mod in modules:
            po_path = os.path.join(BASE, mod, 'i18n', 'ar_001.po')
            changed = apply_to_po(po_path, english, arabic, args.dry_run)
            status = '✓ updated' if changed else '— no match / already correct'
            print(f'  {mod}: "{english[:60]}" → {status}')
            if changed:
                dirty_modules.add(mod)

    if not dirty_modules:
        print('\nNothing changed.')
        return

    print(f'\nImporting {len(dirty_modules)} changed file(s) into DB ({DB})...')
    for mod in sorted(dirty_modules):
        po_path = os.path.join(BASE, mod, 'i18n', 'ar_001.po')
        print(f'  {mod}:')
        import_po(po_path, args.dry_run)

    print('\nDone. Changes are live — no service restart required.')
    print('To make them survive the next module upgrade, commit the updated .po files to git.')


if __name__ == '__main__':
    main()
