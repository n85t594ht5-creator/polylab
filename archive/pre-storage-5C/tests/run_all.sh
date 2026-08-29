#!/bin/bash
# Полный набор тестов POLYLAB. Сеть не используется — источники замоканы.
cd "$(dirname "$0")/.."
declare -A GROUP=( [test_collector.py]="collector" [test_quality.py]="data-quality" \
                   [test_leakage.py]="leakage" [test_storage.py]="storage" )
TOTAL=0; PASSED=0; FAILED=0
for t in test_collector.py test_quality.py test_leakage.py test_storage.py; do
  OUT=$(timeout 300 python3 "tests/$t" 2>&1)
  LINE=$(echo "$OUT" | grep -E "^(COLLECTOR|QUALITY|LEAKAGE|STORAGE):" | tail -1)
  echo "$OUT" | grep -E "^ FAIL" | head -20
  P=$(echo "$LINE" | grep -oE "[0-9]+/[0-9]+" | cut -d/ -f1)
  N=$(echo "$LINE" | grep -oE "[0-9]+/[0-9]+" | cut -d/ -f2)
  printf "%-14s %s\n" "${GROUP[$t]}" "$LINE"
  TOTAL=$((TOTAL+N)); PASSED=$((PASSED+P)); FAILED=$((FAILED+N-P))
done
echo
echo "════════════════════════════════"
echo "TOTAL TESTS: $TOTAL"
echo "PASSED:      $PASSED"
echo "FAILED:      $FAILED"
[ "$FAILED" -eq 0 ] || exit 1
