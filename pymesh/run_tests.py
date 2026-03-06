"""
PyMesh Chat — Test Runner
Runs Phase 1 and Phase 2 test suites in sequence.
No pytest required — just: python3 pymesh/run_tests.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from pymesh.tests.test_phase1 import main as p1, results as r1
    from pymesh.tests.test_phase2 import main as p2, results as r2

    await p1()
    await p2()

    total_pass = r1["pass"] + r2["pass"]
    total_fail = r1["fail"] + r2["fail"]
    total      = total_pass + total_fail

    colour = "\033[92m" if total_fail == 0 else "\033[91m"
    print(f"\033[1m━━━  Overall: {colour}{total_pass}/{total} passed\033[0m\n")

    if total_fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
