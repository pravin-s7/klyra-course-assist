#!/usr/bin/env bash
# Usage: ./evaluation_script.sh [path/to/student_solution.{sh,sed,awk,gawk}]
set -uo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
DEFAULT_RUNNER="__DEFAULT_RUNNER__"
SOLUTION=""
for cand in "$ROOT/solution.sh" "$ROOT/script.sh" "$ROOT/solution.sed" "$ROOT/script.sed" "$ROOT/solution.gawk" "$ROOT/script.gawk" "$ROOT/solution.awk" "$ROOT/script.awk"; do
  if [[ -f "$cand" ]]; then
    SOLUTION="$cand"
    break
  fi
done
[[ -z "$SOLUTION" ]] && SOLUTION="$ROOT/solution.sh"
for arg in "$@"; do
  if [[ "$arg" != --* ]]; then
    SOLUTION="$arg"
    break
  fi
done
run_solution_on_file() {
  local sol="$1"
  local infile="$2"
  local first_line=""
  [[ -f "$sol" ]] && IFS= read -r first_line < "$sol" || true
  if [[ "$sol" == *.sed ]] || [[ "$first_line" == "#!"*sed* ]] || [[ "$DEFAULT_RUNNER" == "sed" && "$sol" != *.sh && "$sol" != *.awk && "$sol" != *.gawk ]]; then
    sed -Ef "$sol" < "$infile" 2>/dev/null || true
  elif [[ "$sol" == *.gawk ]] || [[ "$first_line" == "#!"*gawk* ]]; then
    gawk -f "$sol" < "$infile" 2>/dev/null || true
  elif [[ "$sol" == *.awk ]] || [[ "$first_line" == "#!"*awk* ]]; then
    if [[ "$DEFAULT_RUNNER" == "gawk" ]]; then
      gawk -f "$sol" < "$infile" 2>/dev/null || true
    else
      awk -f "$sol" < "$infile" 2>/dev/null || true
    fi
  elif [[ "$DEFAULT_RUNNER" == "gawk" ]]; then
    gawk -f "$sol" < "$infile" 2>/dev/null || true
  elif [[ "$DEFAULT_RUNNER" == "awk" ]]; then
    awk -f "$sol" < "$infile" 2>/dev/null || true
  else
    bash "$sol" < "$infile" 2>/dev/null || true
  fi
}
fail=0
passed=0
total=0
pub_passed=0
pub_total=0
priv_passed=0
priv_total=0
run_bank() {
  local kind=$1
  local dir="$ROOT/$kind"
  [[ -d "$dir" ]] || return 0
  for tc in "$dir"/test_case_*; do
    [[ -d "$tc" ]] || continue
    total=$((total + 1))
    if [[ "$kind" == "public" ]]; then pub_total=$((pub_total + 1)); else priv_total=$((priv_total + 1)); fi
    local got
    got=$(run_solution_on_file "$SOLUTION" "$tc/input.txt")
    local exp
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

