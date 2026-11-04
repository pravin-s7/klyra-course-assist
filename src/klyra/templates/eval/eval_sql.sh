#!/usr/bin/env bash
set -uo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
SOLUTION="$ROOT/solution.sql"
for arg in "$@"; do
  if [[ "$arg" != --* ]]; then
    SOLUTION="$arg"
    break
  fi
done
fail=0
passed=0
total=0
pub_passed=0
pub_total=0
priv_passed=0
priv_total=0
run_sql() {
  local tc_input=$1 sol=$2
  if command -v sqlite3 >/dev/null 2>&1; then
    (cat "$tc_input"; echo ""; cat "$sol") | sqlite3 -batch :memory: 2>/dev/null || true
  else
    python3 - "$tc_input" "$sol" << 'PYEOF'
import sqlite3, sys
from pathlib import Path
conn = sqlite3.connect(":memory:")
setup_sql = Path(sys.argv[1]).read_text()
query_sql = Path(sys.argv[2]).read_text()
if setup_sql.strip():
    conn.executescript(setup_sql)
cur = conn.cursor()
for stmt in [s.strip() for s in query_sql.split(";") if s.strip()]:
    cur.execute(stmt)
    rows = cur.fetchall()
    for r in rows:
        print("|".join("" if v is None else str(v) for v in r))
PYEOF
  fi
}
run_bank() {
  local kind=$1
  local dir="$ROOT/$kind"
  [[ -d "$dir" ]] || return 0
  for tc in "$dir"/test_case_*; do
    [[ -d "$tc" ]] || continue
    total=$((total + 1))
    if [[ "$kind" == "public" ]]; then pub_total=$((pub_total + 1)); else priv_total=$((priv_total + 1)); fi
    got=$(run_sql "$tc/input.txt" "$SOLUTION")
    exp=$(cat "$tc/output.txt")
    if [[ "$(printf '%s' "$got" | sed 's/[[:space:]]*$//')" == "$(printf '%s' "$exp" | sed 's/[[:space:]]*$//')" ]]; then
      echo "PASS $(basename "$tc") ($kind)"
      passed=$((passed + 1))
      if [[ "$kind" == "public" ]]; then pub_passed=$((pub_passed + 1)); else priv_passed=$((priv_passed + 1)); fi
    else
      echo "FAIL $(basename "$tc") ($kind)"
      diff --color=always <(printf '%s\n' "$got" | sed 's/[[:space:]]*$//') <(printf '%s\n' "$exp" | sed 's/[[:space:]]*$//') || true
      fail=1
    fi
  done
}
run_bank public
run_bank private
echo "Public: $pub_passed/$pub_total passed"
echo "Private: $priv_passed/$priv_total passed"
if [[ $fail -eq 0 ]]; then
  echo "ALL PASSED ($passed/$total)"
else
  echo "$passed/$total passed"
fi
exit $fail

