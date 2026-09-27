# -*- coding: utf-8 -*-
"""
把 data/exam_intake/*.md（人工填寫的考題模板）轉成正式的
data/exam_questions.json（考題 LIFF 用的題庫格式）。

用法：
    python scripts/import_exam_questions.py                # 預覽結果，不寫檔
    python scripts/import_exam_questions.py --write         # 實際寫入 data/exam_questions.json

規則：
- 只處理 data/exam_intake/ 底下、檔名不是 TEMPLATE_ 開頭的 .md 檔
  （TEMPLATE_*.md 是給人複製的空白模板，不當成真的資料）
- 每個檔案第一行「# 113年 第一次」解析出 exam_year / exam_session
- 「考試類別：...」解析出 exam_type
- 每個「## Q<任意文字>」區塊解析出章節／題型／題目／選項／答案
- id 用「{exam_year}-{exam_session流水碼}-{章節縮寫}-{檔內流水號}」規則產生
- concepts 欄位固定留空陣列，之後由考點分群腳本填入，這支腳本不處理
- 缺欄位（章節、題目、答案任一個空白）的題目會被跳過，並在結尾列出警告，
  不會讓整個匯入因為一題沒填完就失敗
"""
import argparse
import json
import os
import re
import sys

INTAKE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "exam_intake")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "exam_questions.json")

_SESSION_CODE = {"第一次": "1", "第二次": "2", "第三次": "3"}


def _session_code(session_label):
    return _SESSION_CODE.get((session_label or "").strip(), "0")


def _parse_file(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    # 拿掉 HTML 註解（填寫說明）
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    header_match = re.search(r"^#\s*(\d+)\s*年\s*(第[一二三四五]次)", text, re.MULTILINE)
    if not header_match:
        print(f"[SKIP] {os.path.basename(path)}：找不到「# 113年 第一次」這種標題格式")
        return []
    exam_year, exam_session = header_match.group(1), header_match.group(2)

    type_match = re.search(r"考試類別[：:]\s*(.+)", text)
    exam_type = type_match.group(1).strip() if type_match else ""

    blocks = re.split(r"^##\s*Q\S*", text, flags=re.MULTILINE)[1:]
    questions = []
    warnings = []
    for i, block in enumerate(blocks, start=1):
        def _field(label, multiline=False):
            pattern = rf"{label}[：:]\s*(.+?)(?=\n[一-龥A-Za-z]+[：:]|\n---|\Z)" if multiline \
                else rf"{label}[：:]\s*(.+)"
            m = re.search(pattern, block, re.DOTALL if multiline else 0)
            return (m.group(1).strip() if m else "")

        category = _field("章節")
        qtype = _field("題型")
        question_text = _field("題目", multiline=True)
        options_block = _field("選項", multiline=True)
        answer = _field("答案").strip().upper()

        options = {}
        for line in options_block.splitlines():
            m = re.match(r"\s*([A-Za-z])[.．：:、]\s*(.+)", line)
            if m:
                options[m.group(1).upper()] = m.group(2).strip()

        if not category or not question_text or not options or not answer:
            warnings.append(f"{os.path.basename(path)} 第 {i} 題資料不完整，已跳過")
            continue

        cat_code = {"基礎理論": "A", "中藥學": "B", "方劑學": "C", "針灸學": "D", "辨證論治": "E"}.get(category, "X")
        qid = f"{exam_year}-{_session_code(exam_session)}-{cat_code}-{i:03d}"

        questions.append({
            "id": qid,
            "exam_year": exam_year,
            "exam_session": exam_session,
            "exam_type": exam_type,
            "category": category,
            "question_type": qtype or "單選題",
            "question": question_text,
            "options": options,
            "answer": answer,
            "explanation": None,
            "concepts": [],
        })

    for w in warnings:
        print(f"[WARN] {w}")
    return questions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="實際寫入 data/exam_questions.json（不加這個參數只會預覽）")
    args = parser.parse_args()

    if not os.path.isdir(INTAKE_DIR):
        print(f"找不到資料夾：{INTAKE_DIR}")
        sys.exit(1)

    files = sorted(
        f for f in os.listdir(INTAKE_DIR)
        if f.endswith(".md") and not f.startswith("TEMPLATE_")
    )
    if not files:
        print("data/exam_intake/ 底下沒有找到任何非 TEMPLATE_ 開頭的 .md 檔，沒有東西可以匯入。")
        sys.exit(0)

    all_questions = []
    for fname in files:
        qs = _parse_file(os.path.join(INTAKE_DIR, fname))
        print(f"{fname}: 解析出 {len(qs)} 題")
        all_questions.extend(qs)

    print(f"\n總計 {len(all_questions)} 題")
    if all_questions:
        print("範例第一題：")
        print(json.dumps(all_questions[0], ensure_ascii=False, indent=2))

    if args.write:
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(all_questions, f, ensure_ascii=False, indent=2)
        print(f"\n已寫入 {OUTPUT_PATH}")
    else:
        print("\n（這是預覽模式，沒有寫入檔案。確認沒問題後加 --write 參數正式寫入。）")


if __name__ == "__main__":
    main()
