# -*- coding: utf-8 -*-
"""驗證 is_off_topic 攔截邏輯。"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.syllabus import is_off_topic

CASES = [
    ("陰陽的概念", False),
    ("手陽明經", False),
    ("明天天氣", True),
    ("合谷穴在哪", False),
    ("足太陰脾經", False),
]

if __name__ == "__main__":
    ok = 0
    for q, expected in CASES:
        got = is_off_topic(q)
        status = "OK" if got == expected else "FAIL"
        if got == expected:
            ok += 1
        print(f"{status}: is_off_topic('{q}') = {got} (expected {expected})")
    print(f"\n{ok}/{len(CASES)} passed")
    sys.exit(0 if ok == len(CASES) else 1)
