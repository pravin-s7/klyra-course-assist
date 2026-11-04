#!/usr/bin/env python3
"""Usage: python3 evaluation_script.py [path/to/student_solution.py]"""
import subprocess, sys, tempfile
from pathlib import Path

def show_diff(got: str, exp: str) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False) as fg, tempfile.NamedTemporaryFile("w", delete=False) as fe:
        fg.write(got + "\n")
        fe.write(exp + "\n")
        g_path, e_path = fg.name, fe.name
    try:
        subprocess.run(["diff", "--color=always", g_path, e_path])
    finally:
        Path(g_path).unlink(missing_ok=True)
        Path(e_path).unlink(missing_ok=True)

root = Path(__file__).resolve().parent
pos_args = [a for a in sys.argv[1:] if not a.startswith("-")]
solution = pos_args[0] if pos_args else str(root / "solution.py")
fail = 0
passed = 0
total = 0
counts = {"public": [0, 0], "private": [0, 0]}
for kind in ("public", "private"):
    bank = root / kind
    if not bank.is_dir():
        continue
    for tc in sorted(bank.glob("test_case_*")):
        if not tc.is_dir():
            continue
        total += 1
        counts[kind][1] += 1
        inp = (tc / "input.txt").read_text()
        exp = "\n".join(line.rstrip() for line in (tc / "output.txt").read_text().rstrip().splitlines())
        proc = subprocess.run([sys.executable, solution], input=inp, text=True, capture_output=True, cwd=str(root))
        got = "\n".join(line.rstrip() for line in (proc.stdout or "").rstrip().splitlines())
        status = "PASS" if got == exp and proc.returncode == 0 else "FAIL"
        print(f"{status} {tc.name} ({kind})", flush=True)
        if status == "FAIL":
            fail = 1
            show_diff(got, exp)
        else:
            passed += 1
            counts[kind][0] += 1
print(f"Public: {counts['public'][0]}/{counts['public'][1]} passed")
print(f"Private: {counts['private'][0]}/{counts['private'][1]} passed")
if fail == 0:
    print(f"ALL PASSED ({passed}/{total})")
else:
    print(f"{passed}/{total} passed")
sys.exit(fail)

