# -*- coding: utf-8 -*-
"""
國考題庫練習系統：題庫載入、作答評分、錯題本管理。

題庫內容存於 data/exam_questions.json，做法比照 tcm_master_knowledge.json——
啟動時載入記憶體快取，之後只讀不寫（題庫更新＝直接改 JSON 檔案重新部署）。
使用者的錯題本與作答紀錄則寫入 MongoDB（比照 research_logging.py 既有模式），
因為這是每個使用者自己的、會持續變動的資料。

現況：題庫只有 20 題（來自台灣中醫師執照考古題），且只有 category（章節）
分類，尚未有「依年度」所需的年份/級別 metadata，所以目前只實作依章節篩選。
之後題庫擴充、補上年度欄位後，再加開「依年度」的篩選邏輯。
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


def get_questions_by_category(category=None):
    """
    回傳指定章節的題目，給前端作答用——不含正確答案與詳解，避免使用者直接看到答案。
    category 為 None 或空字串時回傳全部題目。
    """
    out = []
    for q in load_questions():
        if category and q.get("category") != category:
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
        "question": q.get("question"),
        "options": q.get("options"),
        "answer": q.get("answer"),
        "explanation": q.get("explanation"),
    }


def submit_exam(db, user_id, category, answers):
    """
    交卷評分。answers: {question_id: chosen_letter}。
    回傳 {"score": int(0-100), "correct_count", "total", "results": [...]}。
    只有正常交卷才會呼叫這個函式（中途關閉不呼叫＝不留紀錄，交由前端控制）。
    寫入 exam_sessions 留存這次考試紀錄；答錯的題目自動加入錯題本（exam_wrong_questions）。
    """
    results = []
    correct_count = 0
    for qid, chosen in (answers or {}).items():
        q = _question_by_id(qid)
        if not q:
            continue
        chosen_norm = (chosen or "").strip().upper()
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
                "question_ids": list((answers or {}).keys()),
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
