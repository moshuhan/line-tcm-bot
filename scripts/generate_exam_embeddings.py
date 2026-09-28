# -*- coding: utf-8 -*-
"""
一次性腳本：對 data/exam_questions.json（近十年中醫師國考題庫，5000+ 題）產生 embedding，
供聊天室中醫問答做語意檢索用（讓 AI 回答時優先參考近十年國考題目與官方正解，而不是
純憑自己的知識作答）。

跟 scripts/generate_embeddings.py（tcm_master_knowledge.json 用，只有 20 幾筆）不一樣的地方：
1. 題目量大很多（5000+），用 batch 方式呼叫 embeddings API，不是逐題呼叫。
2. 不能直接比照舊腳本把 embedding 存進一般 JSON 文字檔——1536 維浮點數 x 5254 題，
   算下來 JSON 文字格式會超過 150MB，git／Railway 部署都會很吃力。改成：
   - 用 `dimensions=512` 縮短向量（OpenAI v3 embedding 支援直接截短，語意檢索品質
     幾乎不受影響，因為我們只需要粗略排序 Top-3，不是精密比對）
   - 向量存成 numpy 二進位檔 data/exam_embeddings.npy（float32），不用 JSON 文字
   - 另外只存一份對應的題號清單 data/exam_embeddings_ids.json（跟 .npy 同一列順序），
     執行期用這份題號去對回 exam_questions.json 裡的完整題目內容，不重複存一份
   這樣整體降到 10 幾 MB，跟 exam_questions.json 本身（4MB）同一個量級。

執行方式：
  python scripts/generate_exam_embeddings.py

需要 .env 裡有 OPENAI_API_KEY。
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import numpy as np
from openai import OpenAI

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMENSIONS = 512  # 截短向量維度，兼顧檔案大小與檢索品質
BATCH_SIZE = 200  # 每批送幾題給 embeddings API，避免單次請求過大
ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
INPUT_PATH = DATA_DIR / "exam_questions.json"
OUTPUT_NPY_PATH = DATA_DIR / "exam_embeddings.npy"
OUTPUT_IDS_PATH = DATA_DIR / "exam_embeddings_ids.json"


def _question_to_text(q: dict) -> str:
    """只把題目本文＋選項＋概念/章節放進去 embed（不含答案字母），
    這樣檢索時是用「題目在問什麼」去比對語意，答案在執行期另外查 exam_questions.json 取得。"""
    parts = [q.get("question", "").strip()]
    options = q.get("options") or {}
    for letter in ("A", "B", "C", "D"):
        if options.get(letter):
            parts.append(f"{letter}. {options[letter]}")
    if q.get("chapter"):
        parts.append(f"章節：{q['chapter']}")
    concepts = q.get("concepts") or []
    if concepts:
        parts.append("概念：" + "、".join(concepts))
    return "\n".join(p for p in parts if p and p.strip())


def main():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY 未設定")
        sys.exit(1)

    client = OpenAI(api_key=api_key)

    if not INPUT_PATH.is_file():
        print(f"ERROR: 找不到 {INPUT_PATH}")
        sys.exit(1)
    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        questions = json.load(f)
    if not isinstance(questions, list) or not questions:
        print("ERROR: exam_questions.json 是空的或格式不對")
        sys.exit(1)

    print(f"共 {len(questions)} 題，開始批次產生 embedding（每批 {BATCH_SIZE} 題，{EMBED_DIMENSIONS} 維）...")

    ids = []
    vectors = []
    total_batches = (len(questions) + BATCH_SIZE - 1) // BATCH_SIZE
    for b in range(total_batches):
        batch = questions[b * BATCH_SIZE: (b + 1) * BATCH_SIZE]
        texts = [_question_to_text(q) for q in batch]
        valid = [(q, t) for q, t in zip(batch, texts) if t.strip()]
        if not valid:
            continue
        try:
            resp = client.embeddings.create(
                model=EMBED_MODEL,
                input=[t for _, t in valid],
                dimensions=EMBED_DIMENSIONS,
            )
        except Exception as e:
            print(f"  [批次 {b+1}/{total_batches}] ERROR: {e}")
            continue
        for (q, _), item in zip(valid, resp.data):
            ids.append(q.get("id"))
            vectors.append(item.embedding)
        print(f"  [批次 {b+1}/{total_batches}] OK：累計 {len(ids)} 題")

    if not vectors:
        print("ERROR: 沒有任何題目成功產生 embedding")
        sys.exit(1)

    matrix = np.array(vectors, dtype="float32")
    np.save(OUTPUT_NPY_PATH, matrix)
    with open(OUTPUT_IDS_PATH, "w", encoding="utf-8") as f:
        json.dump(ids, f, ensure_ascii=False)

    npy_size_mb = OUTPUT_NPY_PATH.stat().st_size / (1024 * 1024)
    print(f"\n完成！{len(ids)} 題已存至：")
    print(f"  {OUTPUT_NPY_PATH}（{npy_size_mb:.1f} MB，shape={matrix.shape}）")
    print(f"  {OUTPUT_IDS_PATH}")


if __name__ == "__main__":
    main()
