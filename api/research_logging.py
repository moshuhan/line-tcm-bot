# -*- coding: utf-8 -*-
"""
研究用資料記錄：User / Interaction、意圖與複雜度分類、行為分析。
所有寫入均 guard mongo_db，失敗不影響主流程。

原本這裡還有 QuizResult、StudentFeedback（課務查詢用）、Speaking/Writing 模式的
專屬記錄函式、以及依測驗紀錄產生複習筆記的功能，都隨小測驗／口說練習／寫作修改／
課務查詢這些聊天室舊功能一起移除了。interactions collection 本身繼續保留給
中醫問答模組使用。
"""

import json
from datetime import datetime, timezone

# 集合名稱
COLL_USERS = "users"
# interactions 有效欄位：user_id, mode, question, answer, timestamp, feedback_requested,
# intent_tag (str, LLM 分類), complexity_score, session_duration_sec, follow_up_count
COLL_INTERACTIONS = "interactions"
COLL_FEEDBACK = "feedback"

# 模式
MODES = ("QA", "Speaking", "Writing")

# 學習標籤：研究用 intent_tag
INTENT_TAGS = ("Memory", "Understanding", "Application")

# 當 LLM 分類失敗或非預期值時，intent_tag 的預設值（確保 MongoDB 欄位不為空）
DEFAULT_INTENT_TAG = "General"


def _decode(val):
    if val is None:
        return None
    if hasattr(val, "decode"):
        return val.decode("utf-8", errors="replace")
    return str(val)


def ensure_user(db, user_id):
    """若無則建立 User 文件，有則更新 last_seen。回傳該 user 的 interaction 總數（用於第 20 筆 feedback）。"""
    if db is None or not user_id:
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
    if db is None or not user_id:
        return
    try:
        db[COLL_USERS].update_one(
            {"user_id": _decode(user_id)},
            {"$inc": {"interaction_count": 1}, "$set": {"updated_at": datetime.now(timezone.utc)}}
        )
    except Exception as e:
        print(f"[research_logging] increment_user_interaction_count error: {e}")


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
    if db is None or not user_id:
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


def get_interaction_count(db, user_id):
    """回傳該使用者的 Interaction 總數（含本筆前）。"""
    if db is None or not user_id:
        return 0
    try:
        return db[COLL_INTERACTIONS].count_documents({"user_id": _decode(user_id)})
    except Exception as e:
        print(f"[research_logging] get_interaction_count error: {e}")
        return 0


def get_last_n_interactions(db, user_id, n=10):
    """回傳該使用者最近 n 筆 Interaction 文件列表（由新到舊）。"""
    if db is None or not user_id or n <= 0:
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
    if db is None or not user_id:
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
    if db is None or not user_id:
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


def classify_user_behavior(db, user_id):
    """
    根據最近互動歷史推斷行為模式：active_explorer（廣泛探索）或 task_oriented（任務導向）。
    寫回 User 的 behavior_pattern。
    """
    if db is None or not user_id:
        return None
    try:
        recent = get_last_n_interactions(db, user_id, 20)
        if not recent:
            return None
        # 簡單啟發：若互動數多、模式多元則視為 active_explorer；否則 task_oriented
        modes = [r.get("mode") for r in recent if r.get("mode")]
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
    if db is None or not user_id:
        return
    try:
        increment_user_interaction_count(db, user_id)
        classify_user_behavior(db, user_id)
    except Exception as e:
        print(f"[research_logging] run_analytics_middleware error: {e}")
