"""Unified test runner executing each test module in isolated processes.

Solves the Windows proactor event loop / Playwright sync runner discovery lockup
and provides a clean CLI entrypoint for both local developers and CI.
"""
import sys
import subprocess
from pathlib import Path

TEST_MODULES = [
    "tests.test_llm_gateway",
    "tests.test_security",
    "tests.test_cdp_e2e_flow",
    "tests.test_extension_scripts",
]

def main() -> int:
    backend_dir = Path(__file__).resolve().parent / "backend"
    print("=" * 70)
    print("Running Unified Web-Frame Test Suite (Isolated Subprocess Execution)")
    print("=" * 70)

    total_failures = 0
    # Use the active python interpreter or the backend virtualenv interpreter
    backend_venv_python = backend_dir / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / ("python.exe" if sys.platform == "win32" else "python")
    py_exec = str(backend_venv_python) if backend_venv_python.exists() else sys.executable

    total_failures = 0
    for mod in TEST_MODULES:
        print(f"\n[Running Module] {mod} ...")
        cmd = [py_exec, "-m", "unittest", mod]
        res = subprocess.run(cmd, cwd=str(backend_dir))
        if res.returncode != 0:
            print(f"FAILED: {mod} exited with returncode {res.returncode}")
            total_failures += 1
        else:
            print(f"PASSED: {mod}")

    print("\n" + "=" * 70)
    if total_failures == 0:
        print(f"ALL {len(TEST_MODULES)} TEST MODULES PASSED SUCCESSFULLY.")
        print("=" * 70)
        return 0
    else:
        print(f"{total_failures} / {len(TEST_MODULES)} TEST MODULES FAILED.")
        print("=" * 70)
        return 1

if __name__ == "__main__":
    sys.exit(main())
