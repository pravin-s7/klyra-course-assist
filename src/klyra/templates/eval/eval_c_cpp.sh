#!/usr/bin/env bash
set -uo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
SOLUTION="$ROOT/solution.{ext}"
for arg in "$@"; do
  if [[ "$arg" != --* ]]; then
    SOLUTION="$arg"
    break
  fi
done
WORKDIR=$(mktemp -d)
BIN="$WORKDIR/solution"
trap 'rm -rf "$WORKDIR"' EXIT
{compiler} -O2 -o "$BIN" "$SOLUTION" -lm || exit 2
fail=0
passed=0
total=0
pub_passed=0
pub_total=0
priv_passed=0
priv_total=0
run_bank() {{
  local kind=$1
  local dir="$ROOT/$kind"
  [[ -d "$dir" ]] || return 0
  for tc in "$dir"/test_case_*; do
    [[ -d "$tc" ]] || continue
    total=$((total + 1))
    if [[ "$kind" == "public" ]]; then pub_total=$((pub_total + 1)); else priv_total=$((priv_total + 1)); fi
    got=$("$BIN" < "$tc/input.txt" 2>/dev/null || true)
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
}}
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

