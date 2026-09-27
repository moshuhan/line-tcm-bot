# -*- coding: utf-8 -*-
"""
國考題庫練習系統：題庫載入、作答評分、錯題本管理。

題庫內容存於 data/exam_questions.json，做法比照 tcm_master_knowledge.json——
啟動時載入記憶體快取，之後只讀不寫（題庫更新＝直接改 JSON 檔案重新部署）。
使用者的錯題本與作答紀錄則寫入 MongoDB（比照 research_logging.py 既有模式），
因為這是每個使用者自己的、會持續變動的資料。

現況：題庫是 105～115 年台灣中醫師執照考古題（共 5254 題，見
scripts/import_exam_pdfs.py／scripts/classify_exam_subjects.py 的匯入與分類流程）。
category 欄位是逐題 AI 分類的細科目（例如「內經」「傷寒論」「中醫內科學」），
chapter 是科目底下更細的子項，concepts 是這題的考點關鍵字，ambiguous 標記
AI 自己覺得不太確定的分類，這幾個欄位都還沒有人工複核過，之後要調整
分類、合併/拆分科目，直接改 data/exam_questions.json 這幾個欄位即可，
不用改這支程式。exam_year／exam_session／exam_stage 已經有年份/梯次
metadata，但「依年度」篩選的前端還沒做，目前仍只實作依章節（category）篩選。
"""
import os
import json
from datetime import datetime, timezone

COLL_EXAM_WRONG = "exam_wrong_questions"
COLL_EXAM_SESSIONS = "exam_sessions"

_QUESTIONS_CACHE = None


def _questions_path():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "exam_questions.json"))


def load_questions():
    """載入題庫至記憶體快取，回傳 list[dict]。檔案不存在或格式錯誤則回傳空 list。"""
    global _QUESTIONS_CACHE
    if _QUESTIONS_CACHE is not None:
        return _QUESTIONS_CACHE
    path = _questions_path()
    if not os.path.isfile(path):
        _QUESTIONS_CACHE = []
        return _QUESTIONS_CACHE
    try:
        with open(path, "r", encoding="utf-8") as f:
            _QUESTIONS_CACHE = json.load(f)
    except Exception:
        _QUESTIONS_CACHE = []
    return _QUESTIONS_CACHE


def list_categories():
    """回傳題庫目前有的章節分類清單，依題庫內出現順序。"""
    seen = []
    for q in load_questions():
        c = q.get("category")
        if c and c not in seen:
            seen.append(c)
    return seen


def list_exam_periods():
    """
    回傳「依年度」可選的梯次清單：每個年度＋階段一組（同一年度的 exam_session 是
    固定值，105-111 年為「第一次」，112 年後為「第二次」，見 PDF 表頭），依年度
    新到舊排序，第一階段排在第二階段前面。每組附上題數，排除 requires_image 題目
    （理由同 get_questions_by_category）。
    """
    counts = {}
    for q in load_questions():
        if q.get("requires_image"):
            continue
        key = (q.get("exam_year"), q.get("exam_session"), q.get("exam_stage"))
        if not all(key):
            continue
        counts[key] = counts.get(key, 0) + 1
    periods = [
        {
            "exam_year": year, "exam_session": session, "exam_stage": stage,
            "label": f"{year}年 {session}．{stage}",
            "count": count,
        }
        for (year, session, stage), count in counts.items()
    ]
    periods.sort(key=lambda p: (-int(p["exam_year"]), p["exam_stage"]))
    return periods


def get_questions_by_period(exam_year, exam_session, exam_stage):
    """
    回傳指定梯次（年度＋考次＋階段）的題目，給前端作答用——不含正確答案。
    對應線框圖「依年度」篩選：使用者選的是一個實際考卷梯次，不是章節。
    """
    out = []
    for q in load_questions():
        if q.get("requires_image"):
            continue
        if q.get("exam_year") != exam_year or q.get("exam_stage") != exam_stage:
            continue
        if exam_session and q.get("exam_session") != exam_session:
            continue
        out.append({
            "id": q.get("id"),
            "category": q.get("category"),
            "question": q.get("question"),
            "options": q.get("options"),
        })
    return out


def get_questions_by_category(category=None):
    """
    回傳指定章節的題目，給前端作答用——不含正確答案與詳解，避免使用者直接看到答案。
    category 為 None 或空字串時回傳全部題目。
    刻意排除 requires_image 的題目：這些題目的題幹依賴考卷上的附圖才能作答，
    我們目前沒有附圖檔案，出給使用者會沒辦法作答，之後有圖檔了再拿掉這個篩選。
    """
    out = []
    for q in load_questions():
        if category and q.get("category") != category:
            continue
        if q.get("requires_image"):
            continue
        out.append({
            "id": q.get("id"),
            "category": q.get("category"),
            "question": q.get("question"),
            "options": q.get("options"),
        })
    return out


def _question_by_id(qid):
    for q in load_questions():
        if q.get("id") == qid:
            return q
    return None


def get_question_detail(qid):
    """回傳單題完整內容（含正確答案），給詳解／複習頁使用。"""
    q = _question_by_id(qid)
    if not q:
        return None
    return {
        "id": q.get("id"),
        "category": q.get("category"),
        "chapter": q.get("chapter"),
        "concepts": q.get("concepts"),
        "exam_year": q.get("exam_year"),
        "exam_session": q.get("exam_session"),
        "question": q.get("question"),
        "options": q.get("options"),
        "answer": q.get("answer"),
        "explanation": q.get("explanation"),
    }


def submit_exam(db, user_id, category, question_ids, answers):
    """
    交卷評分。question_ids：這次出給使用者的完整題目清單（不管有沒有作答都要算進分母，
    這是這次考卷的真實題數）。answers: {question_id: chosen_letter}，沒有作答的題目
    視為答錯，不是「不計分」——分數才會真的反映「這整份考卷答對幾題」，而不是
    「使用者隨便點幾題就能拿高分」。
    回傳 {"score": int(0-100), "correct_count", "total", "results": [...]}。
    只有正常交卷才會呼叫這個函式（中途關閉不呼叫＝不留紀錄，交由前端控制）。
    寫入 exam_sessions 留存這次考試紀錄；答錯（含未作答）的題目自動加入錯題本
    （exam_wrong_questions）。
    """
    results = []
    correct_count = 0
    answers = answers or {}
    for qid in (question_ids or []):
        q = _question_by_id(qid)
        if not q:
            continue
        chosen_norm = (answers.get(qid) or "").strip().upper()
        correct_answer = (q.get("answer") or "").strip().upper()
        is_correct = bool(chosen_norm) and chosen_norm == correct_answer
        if is_correct:
            correct_count += 1
        results.append({
            "id": qid,
            "category": q.get("category"),
            "question": q.get("question"),
            "options": q.get("options"),
            "your_answer": chosen_norm,
            "correct_answer": correct_answer,
            "is_correct": is_correct,
        })

    total = len(results)
    score = round(correct_count / total * 100) if total else 0

    if db is not None and user_id and total:
        try:
            db[COLL_EXAM_SESSIONS].insert_one({
                "user_id": user_id,
                "category": category,
                "score": score,
                "correct_count": correct_count,
                "total": total,
                "question_ids": list(question_ids or []),
                "created_at": datetime.now(timezone.utc),
            })
        except Exception:
            pass
        for r in results:
            if not r["is_correct"]:
                try:
                    _add_wrong_question(db, user_id, r["id"], r["category"], source="auto")
                except Exception:
                    pass

    return {"score": score, "correct_count": correct_count, "total": total, "results": results}


def _add_wrong_question(db, user_id, question_id, category, source="manual"):
    if db is None or not user_id or not question_id:
        return
    db[COLL_EXAM_WRONG].update_one(
        {"user_id": user_id, "question_id": question_id},
        {
            "$set": {
                "user_id": user_id,
                "question_id": question_id,
                "category": category,
                "source": source,
                "updated_at": datetime.now(timezone.utc),
            },
            "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
        },
        upsert=True,
    )


def toggle_bookmark(db, user_id, question_id):
    """
    作答當下手動標記／取消標記某題到錯題本（對應線框圖右上角的標記符號）。
    回傳 True＝已加入錯題本，False＝已移除，None＝參數錯誤或無資料庫連線。
    """
    if db is None or not user_id or not question_id:
        return None
    existing = db[COLL_EXAM_WRONG].find_one({"user_id": user_id, "question_id": question_id})
    if existing:
        db[COLL_EXAM_WRONG].delete_one({"_id": existing["_id"]})
        return False
    q = _question_by_id(question_id)
    _add_wrong_question(db, user_id, question_id, q.get("category") if q else None, source="manual")
    return True


def list_wrong_questions(db, user_id, category=None):
    """回傳使用者錯題本清單（附完整題目內容含正確答案），可依 category 篩選，最新標記在前。"""
    if db is None or not user_id:
        return []
    query = {"user_id": user_id}
    if category:
        query["category"] = category
    try:
        docs = list(db[COLL_EXAM_WRONG].find(query).sort("updated_at", -1))
    except Exception:
        return []
    out = []
    for d in docs:
        q = _question_by_id(d.get("question_id"))
        if not q:
            continue
        out.append({
            "id": q.get("id"),
            "category": q.get("category"),
            "question": q.get("question"),
            "options": q.get("options"),
            "answer": q.get("answer"),
            "source": d.get("source", "manual"),
        })
    return out


def remove_wrong_question(db, user_id, question_id):
    """從錯題本移除單一題目（對應線框圖再按一次標記圖示）。回傳是否有刪除到資料。"""
    if db is None or not user_id or not question_id:
        return False
    try:
        res = db[COLL_EXAM_WRONG].delete_one({"user_id": user_id, "question_id": question_id})
        return res.deleted_count > 0
    except Exception:
        return False
