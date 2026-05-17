# -*- coding: utf-8 -*-
"""
一次性腳本：對 tcm_master_knowledge.json 所有知識點產生 embedding，
輸出到 data/tcm_embeddings.json。

執行方式：
  python scripts/generate_embeddings.py

需要 .env 裡有 OPENAI_API_KEY。
"""

import json
import os
import sys
import glob
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI

EMBED_MODEL = "text-embedding-3-small"
ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_PATH = DATA_DIR / "tcm_embeddings.json"


def _kp_to_text(kp: dict) -> str:
    parts = []
    if kp.get("category"):
        parts.append(f"【{kp['category']}】")
    for field in ("core_logic", "mechanism", "functions"):
        if kp.get(field):
            parts.append(kp[field])
    for cr in (kp.get("causal_relationships") or []):
        if isinstance(cr, dict):
            parts.append(f"{cr.get('emotion','')}→{cr.get('impact','')}：{cr.get('symptoms','')}")
    for pf in (kp.get("pathological_features") or []):
        if isinstance(pf, dict):
            parts.append(f"{pf.get('evil','')}：{pf.get('features','')}")
    for row in (kp.get("five_elements_table") or []):
        if isinstance(row, dict):
            parts.append(json.dumps(row, ensure_ascii=False))
    for qa in (kp.get("student_qa") or []):
        if isinstance(qa, str):
            parts.append(qa)
    for ii in (kp.get("inspection_items") or []):
        if isinstance(ii, dict):
            parts.append(ii.get("item", "") + ": " + (ii.get("logic") or ", ".join(ii.get("types", []))))
    if kp.get("mapping"):
        for k, v in (kp["mapping"] or {}).items():
            parts.append(f"{k}: {v}")
    for feat in (kp.get("features") or []):
        if isinstance(feat, dict):
            parts.append(feat.get("type", "") + ": " + (feat.get("logic") or ""))
    for item in (kp.get("items") or []):
        if isinstance(item, dict):
            parts.append(f"{item.get('name','')}: {item.get('logic','')}")
    for d in (kp.get("details") or []):
        if isinstance(d, dict):
            label = d.get("type") or d.get("item", "")
            parts.append(f"{label}: {d.get('logic','')}")
    for t in (kp.get("types") or []):
        if isinstance(t, dict):
            parts.append(f"{t.get('name','')}: {t.get('logic','')}")
    for m in (kp.get("methods") or []):
        if isinstance(m, dict):
            parts.append(f"{m.get('name','')}: {m.get('details','')}")
    for tq in (kp.get("ten_questions_logic") or []):
        if isinstance(tq, dict):
            parts.append(f"{tq.get('item','')}: {tq.get('logic','')}")
    if kp.get("pulse_mapping"):
        for k, v in (kp["pulse_mapping"] or {}).items():
            parts.append(f"{k}: {v}")
    for cp in (kp.get("common_pulses") or []):
        if isinstance(cp, dict):
            parts.append(f"{cp.get('pulse','')}: {cp.get('logic','')}")
    for cc in (kp.get("common_conditions") or []):
        if isinstance(cc, str):
            parts.append(cc)
    if kp.get("interactions"):
        for k, v in (kp["interactions"] or {}).items():
            parts.append(f"{k}: {v}")
    return "\n".join(p for p in parts if p.strip())


def main():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY 未設定")
        sys.exit(1)

    client = OpenAI(api_key=api_key)

    # 載入所有 tcm_*.json
    json_paths = glob.glob(str(DATA_DIR / "tcm_*.json"))
    all_kps = []
    for p in json_paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            for kp in (data.get("knowledge_points") or []):
                all_kps.append(kp)
        except Exception as e:
            print(f"  [警告] 無法載入 {p}: {e}")

    if not all_kps:
        print("ERROR: 找不到任何 knowledge_point")
        sys.exit(1)

    print(f"共 {len(all_kps)} 個 knowledge_point，開始產生 embedding...")

    records = []
    for i, kp in enumerate(all_kps, 1):
        text = _kp_to_text(kp)
        if not text.strip():
            print(f"  [{i:02d}] 跳過（無文字）")
            continue
        try:
            resp = client.embeddings.create(model=EMBED_MODEL, input=text)
            embedding = resp.data[0].embedding
            records.append({
                "category": kp.get("category", ""),
                "text": text,
                "embedding": embedding,
            })
            print(f"  [{i:02d}/{len(all_kps)}] OK: {kp.get('category','')[:40]}")
        except Exception as e:
            print(f"  [{i:02d}] ERROR: {e}")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)

    print(f"\n完成！{len(records)} 筆已存至 {OUTPUT_PATH}")
    print(f"向量維度：{len(records[0]['embedding']) if records else 'N/A'}")


if __name__ == "__main__":
    main()
