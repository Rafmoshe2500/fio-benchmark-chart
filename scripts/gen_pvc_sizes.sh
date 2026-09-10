#!/bin/bash
# Regenerate pvc_sizes.conf from the job files. Run after changing any
# size= or numjobs= value.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh
{
  echo "# PVC size per test, computed as size x numjobs x 1.2 headroom."
  echo "# Regenerate with: ./gen_pvc_sizes.sh"
  echo "# Any change to size= or numjobs= in a .fio file must be reflected here;"
  echo "# preflight refuses to deploy when they disagree."
  echo "#"
  printf "# %-38s %s\n" "test_id" "pvc_size"
  for f in ../jobs/tests/*.fio ../jobs/profiles/*.fio; do
    id=$(basename "$f" .fio)
    need=$(python3 fio_capacity.py "$f" | nocr)
    [ "$need" -lt 8 ] && need=$((need + 1))
    printf "%-40s %sGi\n" "$id" "$need"
  done
} > pvc_sizes.conf
echo "pvc_sizes.conf regenerated"
