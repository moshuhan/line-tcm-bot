# -*- coding: utf-8 -*-
"""
逐題 AI 分類：把每題歸到「該試卷包含的科目」其中之一（subject），結果存到
data/exam_intake/subject_labels.json（以題目 id 為 key，可續跑）。

用法：
    python scripts/classify_exam_subjects.py --sample 5     # 每種試卷抽 5 題試跑
    python scripts/classify_exam_subjects.py --all          # 全部題目（已分類的會跳過）
"""
import argparse
import concurrent.futures as cf
import glob
import io
import contextlib
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))
import import_exam_pdfs as pdfs  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
LABELS_PATH = os.path.join(ROOT, "data", "exam_intake", "subject_labels.json")
EXAM_ROOT = "D:/中醫師國考"

PAPER_SUBJECTS = {
    "基礎(一)": ["中醫醫學史", "中醫基礎理論", "內經", "難經"],
    "基礎(二)": ["方劑學", "中藥學"],
    "臨床(一)": ["傷寒論", "溫病學", "金匱要略", "中醫證治學", "中醫診斷學"],
    "臨床(二)": ["中醫內科學", "中醫婦科學", "中醫兒科學"],
    "臨床(三)": ["中醫外科學", "中醫傷科學", "中醫五官科學"],
    "臨床(四)": ["針灸科學"],
}
CODE_TO_PAPER = {
    "1101": "基礎(一)", "1317": "基礎(一)",
    "2101": "基礎(二)", "2317": "基礎(二)",
    "1102": "臨床(一)", "1318": "臨床(一)",
    "2102": "臨床(二)", "2318": "臨床(二)",
    "3102": "臨床(三)", "3318": "臨床(三)",
    "4102": "臨床(四)", "4318": "臨床(四)",
}


def load_all_questions():
    out = []
    for d in sorted(glob.glob(f"{EXAM_ROOT}/1*")):
        for qp, ap in pdfs.find_pairs(d):
            with contextlib.redirect_stdout(io.StringIO()):
                r = pdfs.parse_pair(qp, ap)
            qs = r[0] if isinstance(r, tuple) else r
            out.extend(q for q in qs if len(q["options"]) == 4)
    return out


def load_labels():
    if os.path.exists(LABELS_PATH):
        with open(LABELS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_labels(labels):
    with open(LABELS_PATH, "w", encoding="utf-8") as f:
        json.dump(labels, f, ensure_ascii=False, indent=1)


def classify(client, q):
    paper = CODE_TO_PAPER[q["subject_code"]]
    choices = PAPER_SUBJECTS[paper]
    if len(choices) == 1:
        return {"subject": choices[0], "confidence": 1.0, "paper": paper}
    opts = "\n".join(f"{k}. {v}" for k, v in q["options"].items())
    prompt = (
        f"以下是中醫師國考「{paper}」試卷的一題單選題。這份試卷只包含這些科目：{'、'.join(choices)}。\n"
        "請判斷這題主要屬於哪一個科目。只能從上面的科目清單中選一個，不可自創。\n"
        "回傳 JSON：{\"subject\": \"科目名稱\", \"confidence\": 0到1的數字}。\n\n"
        f"題目：{q['question']}\n{opts}"
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    data = json.loads(resp.choices[0].message.content)
    subject = data.get("subject")
    if subject not in choices:
        return {"subject": None, "confidence": 0.0, "paper": paper, "raw": subject}
    return {"subject": subject, "confidence": float(data.get("confidence", 0)), "paper": paper}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, help="每種試卷抽幾題試跑")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if not args.sample and not args.all:
        ap.error("請指定 --sample N 或 --all")

    from dotenv import load_dotenv
    from openai import OpenAI
    load_dotenv(os.path.join(ROOT, ".env"))
    client = OpenAI()

    questions = load_all_questions()
    print(f"共 {len(questions)} 題（已排除選項不足 4 個的題目）")
    labels = load_labels()

    if args.sample:
        random.seed(7)
        by_paper = {}
        for q in questions:
            by_paper.setdefault(CODE_TO_PAPER[q["subject_code"]], []).append(q)
        todo = []
        for paper, qs in by_paper.items():
            todo.extend(random.sample(qs, min(args.sample, len(qs))))
    else:
        todo = questions
    todo = [q for q in todo if q["id"] not in labels]
    print(f"待分類 {len(todo)} 題")

    done = 0
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(classify, client, q): q for q in todo}
        for fut in cf.as_completed(futs):
            q = futs[fut]
            try:
                labels[q["id"]] = fut.result()
            except Exception as e:
                print(f"[ERR] {q['id']}: {e}")
                continue
            done += 1
            if done % 100 == 0:
                save_labels(labels)
                print(f"  {done}/{len(todo)}")
    save_labels(labels)
    print(f"完成，已寫入 {LABELS_PATH}")


if __name__ == "__main__":
    main()
