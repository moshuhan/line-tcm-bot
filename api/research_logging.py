# -*- coding: utf-8 -*-
"""
研究用資料記錄：User / Interaction / QuizResult / Feedback、意圖與複雜度分類、行為分析。
所有寫入均 guard mongo_db，失敗不影響主流程。
"""

import json
import re
import time
import uuid
from datetime import datetime, timezone

# 集合名稱
COLL_USERS = "users"
# interactions 有效欄位：user_id, mode, question, answer, timestamp, feedback_requested,
# intent_tag (str, LLM 分類), complexity_score, complexity_level, session_duration_sec, follow_up_count,
# quiz_data (object, 測驗結果：user_answer, is_correct, attempted), quiz_answered_at, response_time_sec
COLL_INTERACTIONS = "interactions"
COLL_QUIZ_RESULTS = "quiz_results"
COLL_FEEDBACK = "feedback"
COLL_STUDENT_FEEDBACK = "StudentFeedback"

# 模式 / 測驗類型
MODES = ("QA", "Speaking", "Writing")
QUIZ_TYPES = ("Immediate", "Review")

# 學習標籤：研究用 intent_tag（中醫領域）與 complexity_level
LEARNING_INTENT_TAGS = ("Basic Theory", "Clinical", "Diagnostics", "Treatment", "Pharmacology", "Other")
COMPLEXITY_LEVELS = ("Low", "Medium", "High")
# 向下相容
INTENT_TAGS = ("Memory", "Understanding", "Application")

# 口說模式：用於計算 TCM 專業術語出現次數的詞表（可擴充）
TCM_TERMS_FOR_SPEECH = [
    "中醫", "經絡", "穴位", "氣血", "陰陽", "五行", "臟腑", "肝", "心", "脾", "肺", "腎",
    "望聞問切", "脈診", "舌診", "辨證", "證型", "虛實", "寒熱", "表裡",
    "氣滯", "血瘀", "痰濕", "濕熱", "風寒", "風熱", "氣虛", "血虛",
    "針灸", "艾灸", "拔罐", "推拿", "方劑", "中藥", "四氣五味",
    "十二經脈", "奇經八脈", "任脈", "督脈", "手太陰", "足陽明",
]


def _decode(val):
    if val is None:
        return None
    if hasattr(val, "decode"):
        return val.decode("utf-8", errors="replace")
    return str(val)


def ensure_user(db, user_id):
    """若無則建立 User 文件，有則更新 last_seen。回傳該 user 的 interaction 總數（用於第 20 筆 feedback）。"""
    if not db or not user_id:
        return 0
    try:
        coll = db[COLL_USERS]
        now = datetime.now(timezone.utc)
        u = coll.find_one({"user_id": _decode(user_id)})
        if not u:
            coll.insert_one({
                "user_id": _decode(user_id),
                "last_seen": now,
                "session_duration_sec": 0,
                "interaction_count": 0,
                "behavior_pattern": None,
                "updated_at": now,
            })
            return 0
        coll.update_one(
            {"user_id": _decode(user_id)},
            {"$set": {"last_seen": now, "updated_at": now}}
        )
        return (u.get("interaction_count") or 0)
    except Exception as e:
        print(f"[research_logging] ensure_user error: {e}")
        return 0


def increment_user_interaction_count(db, user_id):
    """將該使用者的 interaction_count +1。"""
    if not db or not user_id:
        return
    try:
        db[COLL_USERS].update_one(
            {"user_id": _decode(user_id)},
            {"$inc": {"interaction_count": 1}, "$set": {"updated_at": datetime.now(timezone.utc)}}
        )
    except Exception as e:
        print(f"[research_logging] increment_user_interaction_count error: {e}")


# 當 LLM 分類失敗或非預期值時，intent_tag 的預設值（確保 MongoDB 欄位不為空）
DEFAULT_INTENT_TAG = "General"


def classify_qa_intent_and_complexity(openai_client, question, timeout_sec=5):
    """
    使用 LLM 將使用者問題分類為 intent_tag (Memory/Understanding/Application) 與 complexity_score (1-5)。
    回傳 (intent_tag, complexity_score)；intent_tag 若無法分類則回傳 DEFAULT_INTENT_TAG，避免欄位被略過。
    """
    if not openai_client or not (question or "").strip():
        return DEFAULT_INTENT_TAG, None
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "你是一位中醫教育研究助理。請僅根據「使用者問題」回傳一個 JSON 物件，不要其他文字。"
                    "欄位：intent_tag（必為 Memory / Understanding / Application 其一）、complexity_score（1-5 整數）。"
                    "Memory=記憶事實；Understanding=理解概念；Application=應用/推理。"
                )},
                {"role": "user", "content": f"使用者問題：{(question or '').strip()[:500]}"},
            ],
            max_tokens=80,
            temperature=0.1,
            timeout=timeout_sec,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return DEFAULT_INTENT_TAG, None
        if "{" in raw and "}" in raw:
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
        obj = json.loads(raw)
        intent = (obj.get("intent_tag") or "").strip()
        if intent not in INTENT_TAGS:
            intent = DEFAULT_INTENT_TAG
        comp = obj.get("complexity_score")
        if comp is not None:
            try:
                comp = int(comp)
                if comp < 1 or comp > 5:
                    comp = None
            except (TypeError, ValueError):
                comp = None
        return intent, comp
    except Exception as e:
        print(f"[research_logging] classify_qa_intent_and_complexity error: {e}")
        return DEFAULT_INTENT_TAG, None


def classify_qa_learning_tags(openai_client, question, timeout_sec=5):
    """
    使用 LLM 將使用者問題分類為研究用學習標籤：
    intent_tag（Basic Theory / Clinical / Diagnostics / Treatment / Pharmacology / Other）、
    complexity_level（Low / Medium / High）。
    回傳 (intent_tag, complexity_level)，失敗回傳 (None, None)。
    """
    if not openai_client or not (question or "").strip():
        return None, None
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "你是一位中醫教育研究助理。請僅根據「使用者問題」回傳一個 JSON，不要其他文字。"
                    "欄位：intent_tag（必為以下其一：Basic Theory, Clinical, Diagnostics, Treatment, Pharmacology, Other）、"
                    "complexity_level（必為 Low, Medium, High 其一）。"
                    "Basic Theory=基礎理論；Clinical=臨床應用；Diagnostics=診斷；Treatment=治法/方藥；Pharmacology=中藥藥性。"
                )},
                {"role": "user", "content": f"使用者問題：{(question or '').strip()[:500]}"},
            ],
            max_tokens=80,
            temperature=0.1,
            timeout=timeout_sec,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if not raw or "{" not in raw or "}" not in raw:
            return None, None
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        obj = json.loads(raw)
        intent = (obj.get("intent_tag") or "").strip()
        if intent not in LEARNING_INTENT_TAGS:
            intent = None
        level = (obj.get("complexity_level") or "").strip()
        if level not in COMPLEXITY_LEVELS:
            level = None
        return intent or None, level or None
    except Exception as e:
        print(f"[research_logging] classify_qa_learning_tags error: {e}")
        return None, None


def log_interaction(
    db,
    user_id,
    mode,
    question,
    answer,
    intent_tag=None,
    complexity_score=None,
    complexity_level=None,
    session_duration_sec=None,
    follow_up_count=None,
    feedback_requested=False,
):
    """
    寫入一筆 Interaction，並可選設定 feedback（每 20 筆）。
    回傳 inserted_id（ObjectId），失敗或無 db 時回傳 None。
    """
    if not db or not user_id:
        return None
    try:
        now = datetime.now(timezone.utc)
        # AI 可能回傳 None；確保 intent_tag 一律為字串，方便 MongoDB 查詢與分析
        intent_tag_val = (intent_tag if (intent_tag and isinstance(intent_tag, str) and intent_tag.strip()) else DEFAULT_INTENT_TAG)
        doc = {
            "user_id": _decode(user_id),
            "mode": mode if mode in MODES else "QA",
            "intent_tag": intent_tag_val,
            "complexity_score": complexity_score,
            "complexity_level": complexity_level,
            "session_duration_sec": session_duration_sec,
            "follow_up_count": follow_up_count,
            "question": (question or "")[:2000],
            "answer": (answer or "")[:4000],
            "timestamp": now,
            "feedback_requested": bool(feedback_requested),
        }
        r = db[COLL_INTERACTIONS].insert_one(doc)
        if feedback_requested and r.inserted_id:
            db[COLL_FEEDBACK].insert_one({
                "user_id": _decode(user_id),
                "interaction_id": r.inserted_id,
                "requested_at": now,
            })
        return r.inserted_id
    except Exception as e:
        print(f"[research_logging] log_interaction error: {e}")
        return None


def log_student_feedback(db, user_id, user_name, score):
    """
    寫入一筆 StudentFeedback。
    欄位：timestamp, userName, userId, score
    """
    if not db or not user_id:
        return
    try:
        s = int(score)
        if s < 1 or s > 5:
            return
    except (TypeError, ValueError):
        return
    try:
        db[COLL_STUDENT_FEEDBACK].insert_one({
            "timestamp": datetime.now(timezone.utc),
            "userName": (user_name or "").strip()[:200] or None,
            "userId": _decode(user_id),
            "score": s,
        })
    except Exception as e:
        print(f"[research_logging] log_student_feedback error: {e}")


def update_interaction_quiz_result(
    db,
    interaction_id,
    user_answer,
    is_correct,
    attempted,
    response_time_sec=None,
):
    """
    更新對應的 interaction 文件，寫入 Learning Outcome（測驗結果）。
    結構分離：Conversation（question, answer, timestamp, intent_tag）與 quiz_data（Learning Outcome）。
    interaction_id 可為 ObjectId、字串或 bytes（Redis 未 decode 時）。
    """
    if not db or interaction_id is None:
        return
    try:
        from bson import ObjectId
        if isinstance(interaction_id, bytes):
            interaction_id = interaction_id.decode("utf-8", errors="replace").strip()
        # Redis 存的是字串；MongoDB _id 為 ObjectId，update_one 必須用 ObjectId 才能匹配
        if isinstance(interaction_id, ObjectId):
            oid = interaction_id
        else:
            oid_str = str(interaction_id).strip()
            oid = ObjectId(oid_str)
        print(f">>> DEBUG: Updating Quiz for ID={oid}")
        now = datetime.now(timezone.utc)
        update = {
            "quiz_data": {
                "user_answer": user_answer,
                "is_correct": bool(is_correct),
                "attempted": bool(attempted),
            },
            "quiz_answered_at": now,
        }
        if response_time_sec is not None:
            update["response_time_sec"] = response_time_sec
        db[COLL_INTERACTIONS].update_one(
            {"_id": oid},
            {"$set": update},
        )
    except Exception as e:
        print(f"[research_logging] update_interaction_quiz_result error: {e}")


def get_interaction_count(db, user_id):
    """回傳該使用者的 Interaction 總數（含本筆前）。"""
    if not db or not user_id:
        return 0
    try:
        return db[COLL_INTERACTIONS].count_documents({"user_id": _decode(user_id)})
    except Exception as e:
        print(f"[research_logging] get_interaction_count error: {e}")
        return 0


def get_last_n_interactions(db, user_id, n=10):
    """回傳該使用者最近 n 筆 Interaction 文件列表（由新到舊）。"""
    if not db or not user_id or n <= 0:
        return []
    try:
        cursor = (
            db[COLL_INTERACTIONS]
            .find({"user_id": _decode(user_id)})
            .sort("timestamp", -1)
            .limit(n)
        )
        return list(cursor)
    except Exception as e:
        print(f"[research_logging] get_last_n_interactions error: {e}")
        return []


def get_last_interaction_timestamp(db, user_id):
    """回傳該使用者最後一筆 interaction 的 timestamp (datetime)，若無則 None。"""
    if not db or not user_id:
        return None
    try:
        doc = (
            db[COLL_INTERACTIONS]
            .find_one({"user_id": _decode(user_id)}, sort=[("timestamp", -1)], projection={"timestamp": 1})
        )
        return doc.get("timestamp") if doc else None
    except Exception as e:
        print(f"[research_logging] get_last_interaction_timestamp error: {e}")
        return None


def get_follow_up_count_within_sec(db, user_id, within_sec=1800):
    """回傳該使用者在 within_sec 秒內的 interaction 數量（用於 follow_up_count，不含本筆）。"""
    if not db or not user_id:
        return 0
    try:
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(seconds=within_sec)
        return db[COLL_INTERACTIONS].count_documents({
            "user_id": _decode(user_id),
            "timestamp": {"$gte": since},
        })
    except Exception as e:
        print(f"[research_logging] get_follow_up_count_within_sec error: {e}")
        return 0


def log_quiz_result(db, user_id, quiz_type, question_id, user_answer, is_correct, response_time_sec=None):
    """寫入 QuizResult。quiz_type 為 Immediate 或 Review。"""
    if not db or not user_id:
        return
    try:
        db[COLL_QUIZ_RESULTS].insert_one({
            "user_id": _decode(user_id),
            "type": quiz_type if quiz_type in QUIZ_TYPES else "Immediate",
            "question_id": (question_id or "")[:200],
            "user_answer": (user_answer or "")[:20],
            "is_correct": bool(is_correct),
            "response_time_sec": response_time_sec,
            "timestamp": datetime.now(timezone.utc),
        })
    except Exception as e:
        print(f"[research_logging] log_quiz_result error: {e}")


def count_tcm_terms_in_text(text):
    """計算 text 中出現的 TCM 專業術語次數（重複出現多次計多次）。"""
    if not (text or "").strip():
        return 0
    text = (text or "").strip()
    count = 0
    for term in TCM_TERMS_FOR_SPEECH:
        if term in text:
            count += text.count(term)
    return count


def log_speaking(db, user_id, transcript_length, tcm_term_count, transcript=None):
    """寫入一筆 Speaking 模式的 Interaction（僅記錄口說相關欄位）。"""
    if not db or not user_id:
        return
    try:
        now = datetime.now(timezone.utc)
        doc = {
            "user_id": _decode(user_id),
            "mode": "Speaking",
            "intent_tag": None,
            "complexity_score": None,
            "session_duration_sec": None,
            "follow_up_count": None,
            "question": (transcript or "")[:2000],
            "answer": None,
            "timestamp": now,
            "feedback_requested": False,
            "speaking_transcript_length": transcript_length,
            "speaking_tcm_term_count": tcm_term_count,
        }
        db[COLL_INTERACTIONS].insert_one(doc)
    except Exception as e:
        print(f"[research_logging] log_speaking error: {e}")


def compute_improvement_index(original, revised):
    """
    計算寫作改進指數：簡單以「修訂後長度/原長度」比例為基礎，若原為 0 則回傳 1.0。
    可依需求改為更複雜的指標（例如編輯距離、文法正確數等）。
    """
    if not (original or "").strip():
        return 1.0
    orig_len = len((original or "").strip())
    rev_len = len((revised or "").strip())
    if orig_len == 0:
        return 1.0
    return round(rev_len / orig_len, 4)


def log_writing(db, user_id, original_text, revised_text, improvement_index=None):
    """寫入一筆 Writing 模式的 Interaction，含 improvement_index。"""
    if not db or not user_id:
        return
    try:
        if improvement_index is None:
            improvement_index = compute_improvement_index(original_text, revised_text)
        now = datetime.now(timezone.utc)
        doc = {
            "user_id": _decode(user_id),
            "mode": "Writing",
            "intent_tag": None,
            "complexity_score": None,
            "session_duration_sec": None,
            "follow_up_count": None,
            "question": (original_text or "")[:2000],
            "answer": (revised_text or "")[:4000],
            "timestamp": now,
            "feedback_requested": False,
            "writing_improvement_index": improvement_index,
        }
        db[COLL_INTERACTIONS].insert_one(doc)
    except Exception as e:
        print(f"[research_logging] log_writing error: {e}")


def classify_user_behavior(db, user_id):
    """
    根據最近互動歷史推斷行為模式：active_explorer（廣泛探索）或 task_oriented（任務導向）。
    寫回 User 的 behavior_pattern。
    """
    if not db or not user_id:
        return None
    try:
        recent = get_last_n_interactions(db, user_id, 20)
        if not recent:
            return None
        # 簡單啟發：若互動數多、模式多元（QA/Speaking/Writing 混用）則視為 active_explorer；否則 task_oriented
        modes = [r.get("mode") for r in recent if r.get("mode")]
        qa_count = sum(1 for m in modes if m == "QA")
        speaking_count = sum(1 for m in modes if m == "Speaking")
        writing_count = sum(1 for m in modes if m == "Writing")
        unique_modes = len(set(modes))
        if unique_modes >= 2 or len(recent) >= 10:
            pattern = "active_explorer"
        else:
            pattern = "task_oriented"
        db[COLL_USERS].update_one(
            {"user_id": _decode(user_id)},
            {"$set": {"behavior_pattern": pattern, "updated_at": datetime.now(timezone.utc)}}
        )
        return pattern
    except Exception as e:
        print(f"[research_logging] classify_user_behavior error: {e}")
        return None


def run_analytics_middleware(db, user_id):
    """在寫入 interaction 後呼叫：更新 User 的 session_duration、interaction_count，並執行行為分類。"""
    if not db or not user_id:
        return
    try:
        increment_user_interaction_count(db, user_id)
        classify_user_behavior(db, user_id)
    except Exception as e:
        print(f"[research_logging] run_analytics_middleware error: {e}")


def generate_review_quiz_from_interactions(db, user_id, openai_client, last_n=10):
    """
    依該使用者最近 last_n 筆 Interaction 內容產生一題個人化複習選擇題。
    回傳與 generate_mcq_quiz 相同結構：{question, options, answer, explanation} 或 None。
    """
    if not db or not user_id or not openai_client:
        return None
    interactions = get_last_n_interactions(db, user_id, last_n)
    if not interactions:
        return None
    context_parts = []
    for i, doc in enumerate(interactions[:10], 1):
        q = (doc.get("question") or "").strip()
        a = (doc.get("answer") or "").strip()
        if q or a:
            context_parts.append(f"[{i}] Q: {q[:200]}\nA: {a[:300]}")
    context = "\n\n".join(context_parts)[:2500]
    if not context.strip():
        return None
    try:
        prompt = f"""
你是一位中醫課程助教。以下是某位學生「最近的問答記錄」。
請根據這些內容出一題「複習用」的三選一選擇題，幫助他鞏固所學。

[學生最近問答]
{context}

[要求]
1. 題目需直接來自上述問答中的概念或重點。
2. 選項共三個，標示為 (A)、(B)、(C)，只有一個正確答案。
3. 回傳格式嚴格為 JSON，不要其他文字：
{{
  "question": "……？",
  "options": ["(A) ……", "(B) ……", "(C) ……"],
  "answer": "A",
  "explanation": "……"
}}
""".strip()
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "你是中醫助教，依學生問答出複習題，僅回傳 JSON。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=250,
            temperature=0.2,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if not raw or "{" not in raw or "}" not in raw:
            return None
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        obj = json.loads(raw)
        question = (obj.get("question") or "").strip()
        options = obj.get("options") or []
        options = [str(x) for x in options][:3] if isinstance(options, list) else []
        answer = str(obj.get("answer") or "").strip().upper()
        if answer not in ("A", "B", "C"):
            answer = "A"
        explanation = (obj.get("explanation") or "").strip()
        if not question or len(options) != 3:
            return None
        return {
            "question": question[:500],
            "options": [o[:200] for o in options],
            "answer": answer,
            "explanation": explanation[:1000],
        }
    except Exception as e:
        print(f"[research_logging] generate_review_quiz_from_interactions error: {e}")
        return None
