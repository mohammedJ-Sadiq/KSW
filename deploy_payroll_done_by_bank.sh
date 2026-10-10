#!/usr/bin/env bash
# Deploy KSW_payroll 19.0.1.25.0 to KSWCO:
#   - delete batch with its payslips (1.24.0, if not deployed yet)
#   - Mark As Done by bank account (1.25.0)
# KSW_commissions (Mark Paid by bank account) is NOT included.
# Run from /home/odoo/Odoo/odoo.
set -u
C=kswco-prod-odoo-1
D=/mnt/extra-addons/KSW/KSW_payroll
P=custom_addons/KSW/KSW_payroll
FILES="__manifest__.py
models/hr_payslip_run.py
views/hr_payslip_views.xml
wizard/__init__.py
wizard/payslip_run_done_wizard.py
wizard/payslip_run_done_wizard_views.xml
security/ir.model.access.csv
static/src/js/payslip_run_delete.js
static/src/js/payslip_run_delete.xml"
# Prod copies reviewed by hand (2026-10-10): prod ahead of git with
# sum="Total" on the bank totals list, which the new version keeps.
REVIEWED="views/hr_payslip_views.xml:0593ede6b69045127c193edac655fdb7"
# Files the 1.24.0 deploy (delete batch) touched: prod may hold that version.
PRIOR="__manifest__.py models/hr_payslip_run.py static/src/js/payslip_run_delete.js static/src/js/payslip_run_delete.xml"

echo "== 1. Audit"
DBV=$(psql -d KSWCO -tAc "select latest_version from ir_module_module where name='KSW_payroll'")
echo "KSWCO DB version: $DBV"
case "$DBV" in 19.0.1.23.0|19.0.1.24.0) ;; *) echo "!! unexpected version, stop"; exit 1;; esac
for m in KSW_deduction KSW_eos_leave KSW_leave_extension; do
  db=$(psql -d KSWCO -tAc "select latest_version from ir_module_module where name='$m'")
  fs=$(docker exec $C grep -oE "'version': *'[^']+'" /mnt/extra-addons/KSW/$m/__manifest__.py | cat | grep -oE "19[.0-9]+")
  echo "$m db=$db files=$fs"
  [ "$db" = "$fs" ] || { echo "!! $m files and DB differ: -u would run its migrations, stop"; exit 1; }
done
BAD=0
for f in $FILES; do
  prod=$(docker exec $C sh -c "md5sum $D/$f 2>/dev/null" | cat | cut -d' ' -f1)
  head=$(git show HEAD:$P/$f 2>/dev/null | md5sum | cut -d' ' -f1)
  mine=$(md5sum $P/$f | cut -d' ' -f1)
  if [ -z "$prod" ]; then echo "new      $f"
  elif [ "$prod" = "$mine" ]; then echo "same     $f"
  elif [ "$prod" = "$head" ]; then echo "=HEAD    $f"
  elif echo " $REVIEWED " | grep -q " $f:$prod "; then echo "reviewed $f"
  elif echo " $PRIOR " | grep -q " $f "; then
    echo "1.24.0?  $f  (prod differs from HEAD; diff prod -> new below, should be this feature only)"
    docker exec $C cat $D/$f | cat | diff - $P/$f | head -80
  else echo "!! PROD LEADS GIT  $f"; BAD=1; fi
done
[ $BAD = 0 ] || { echo "!! a prod file holds changes not in git; stop and review"; exit 1; }
read -r -p "Audit OK? Type yes to deploy: " ok; [ "$ok" = yes ] || exit 1

echo "== 2. Snapshot"
docker exec $C tar czf /var/lib/odoo/KSW_payroll_pre_1250.tgz -C /mnt/extra-addons/KSW KSW_payroll | cat
docker exec $C ls -la /var/lib/odoo/KSW_payroll_pre_1250.tgz | cat

echo "== 3. Copy"
docker exec -u root $C mkdir -p $D/static/src/js | cat
for f in $FILES; do docker cp "$P/$f" "$C:$D/$f"; done
docker exec -u root $C chown -R odoo:odoo $D | cat
for f in $FILES; do
  a=$(docker exec $C md5sum $D/$f | cat | cut -d' ' -f1); b=$(md5sum $P/$f | cut -d' ' -f1)
  [ "$a" = "$b" ] && echo "MATCH $f" || { echo "!! MISMATCH $f, stop"; exit 1; }
done

echo "== 4. Upgrade"
docker exec $C odoo -c /etc/odoo/odoo.conf -d KSWCO -u KSW_payroll --stop-after-init --http-port=18099 2>&1 | cat > ~/kswco_payroll_1250_upgrade.log
grep -E " (ERROR|CRITICAL) " ~/kswco_payroll_1250_upgrade.log | head -20

echo "== 5. Restart"
docker restart $C
sleep 20

echo "== 6. Verify (from the DB, not the log)"
psql -d KSWCO -tAc "select latest_version from ir_module_module where name='KSW_payroll'"   # expect 19.0.1.25.0
psql -d KSWCO -tAc "select model from ir_model where model like 'ksw.payslip.run.done.wizard%'"  # expect 2 rows
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost:8069/web/login             # expect 200
echo "Rollback if needed:"
echo "  docker exec -u root $C sh -c \"rm -rf $D && tar xzf /var/lib/odoo/KSW_payroll_pre_1250.tgz -C /mnt/extra-addons/KSW\" ; then -u KSW_payroll + docker restart"
