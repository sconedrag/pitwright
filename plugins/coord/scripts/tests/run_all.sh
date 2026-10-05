#!/usr/bin/env bash
# run_all.sh — run every tests/test_*.py in this directory, report pass/fail per file,
# exit non-zero if any failed. Plain bash, no `timeout` (not available on macOS by default).
#
# Usage: bash scripts/tests/run_all.sh   (from plugins/coord, or any cwd — paths are
#                                          resolved relative to this script's own location)

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

total=0
failed=0
failed_names=()

out="$(mktemp)"
trap 'rm -f "$out"' EXIT

for f in "$SCRIPT_DIR"/test_*.py; do
  [ -e "$f" ] || continue
  name="$(basename "$f")"
  total=$((total + 1))
  if python3 "$f" >"$out" 2>&1; then
    echo "PASS  $name"
  else
    echo "FAIL  $name"
    failed=$((failed + 1))
    failed_names+=("$name")
    echo "  --- output ---"
    sed 's/^/  /' "$out"
    echo "  --------------"
  fi
done

echo ""
echo "$((total - failed))/$total passed"

if [ "$failed" -gt 0 ]; then
  echo "FAILED: ${failed_names[*]}"
  exit 1
fi

exit 0
