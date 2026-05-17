# -*- coding: utf-8 -*-
"""Verify TCM knowledge load and retrieval latency < 1 second."""
import json
import os
import time

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_TCM_PATH = os.path.join(_DATA_DIR, "tcm_master_knowledge.json")
_EMOTION_KEYWORDS = frozenset(
    {"怒", "生氣", "喜", "思", "悲", "憂", "恐", "害怕", "驚", "情緒", "七情"}
)
_CLIMATE_KEYWORDS = frozenset(
    {"風", "寒", "暑", "濕", "燥", "火", "六邪", "氣候", "濕氣", "燥熱", "風邪", "寒邪"}
)


def _load():
    with open(_TCM_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_injected(text):
    if not (text or "").strip():
        return ""
    data = _load()
    kps = data.get("knowledge_points") or []
    out = []
    need_emotion = any(kw in text for kw in _EMOTION_KEYWORDS)
    need_climate = any(kw in text for kw in _CLIMATE_KEYWORDS)
    for kp in kps:
        if need_emotion:
            cr = kp.get("causal_relationships")
            if cr:
                lines = [
                    f"{r.get('emotion','')}->{r.get('impact','')}:{r.get('symptoms','')}({r.get('target_organ','')})"
                    for r in cr if isinstance(r, dict)
                ]
                out.append("[七情致病] " + ";".join(lines))
                need_emotion = False
        if need_climate:
            pf = kp.get("pathological_features")
            if pf:
                lines = [
                    f"{r.get('evil','')}:{r.get('features','')},症狀如{r.get('symptoms','')}"
                    for r in pf if isinstance(r, dict)
                ]
                out.append("[六邪致病] " + ";".join(lines))
                need_climate = False
        if not need_emotion and not need_climate:
            break
    return "\n".join(out) if out else ""


def test_load_latency():
    t0 = time.perf_counter()
    _load()
    dt = (time.perf_counter() - t0) * 1000
    assert dt < 1000, f"load took {dt:.1f}ms (>1000ms)"
    print(f"load: {dt:.2f} ms")


def test_retrieve_latency():
    cases = [
        ("emotion", "生氣怎麼辦"),
        ("climate", "濕氣重"),
        ("both", "燥熱又害怕"),
    ]
    for label, q in cases:
        t0 = time.perf_counter()
        k = _get_injected(q)
        dt = (time.perf_counter() - t0) * 1000
        assert dt < 1000, f"retrieve ({label}) took {dt:.1f}ms (>1000ms)"
        print(f"{label:8} '{q}' -> {dt:.2f} ms | has_data={bool(k)}")


if __name__ == "__main__":
    test_load_latency()
    test_retrieve_latency()
    print("OK: all latencies < 1s")
