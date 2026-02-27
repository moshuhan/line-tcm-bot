# -*- coding: utf-8 -*-
"""
個人化學習分析與互動：問題記錄、動態小測驗、弱項追蹤、複習筆記。
動態出題依 syllabus_full 本週主題，含狀態機與自動批改。
"""

import json
import time
import traceback
from openai import OpenAI

# 對話狀態
STATE_NORMAL = "normal"
STATE_QUIZ_WAITING = "quiz_waiting"

# Redis key 前綴
QUESTION_LOG_KEY = "question_log"
QUESTION_LOG_MAX = 5000
LAST_QUESTION_KEY = "last_question:{user_id}"
QUIZ_PENDING_KEY = "quiz_pending:{user_id}"
QUIZ_DATA_KEY = "quiz_data:{user_id}"
USER_STATE_KEY = "user_state:{user_id}"
USER_WEAK_KEY = "user_weak:{user_id}"
LAST_REVIEW_ASK_KEY = "last_review_ask:{user_id}"
REVIEW_ASK_COOLDOWN_DAYS = 7


def log_question(redis_client, user_id, text):
    """將使用者提問記錄到 Redis list，供每週報告使用。"""
    if not redis_client or not (text or "").strip():
        return
    try:
        payload = json.dumps({"user_id": user_id, "text": (text or "").strip()[:500], "ts": time.time()})
        redis_client.lpush(QUESTION_LOG_KEY, payload)
        redis_client.ltrim(QUESTION_LOG_KEY, 0, QUESTION_LOG_MAX - 1)
    except Exception:
        traceback.print_exc()


def set_last_question(redis_client, user_id, text):
    """儲存最後一則問題，供蘇格拉底測驗出題用。"""
    if not redis_client:
        return
    try:
        redis_client.set(f"last_question:{user_id}", (text or "").strip()[:500])
    except Exception:
        pass


def get_last_question(redis_client, user_id):
    if not redis_client:
        return None
    try:
        val = redis_client.get(f"last_question:{user_id}")
        if val is None:
            return None
        return val.decode("utf-8") if hasattr(val, "decode") else str(val)
    except Exception:
        return None


def set_quiz_pending(redis_client, user_id, topic_or_question):
    """設定使用者處於「等待測驗回答」狀態（相容舊 API）。"""
    if not redis_client:
        return
    try:
        redis_client.set(f"quiz_pending:{user_id}", (topic_or_question or "")[:500])
    except Exception:
        pass


def get_quiz_pending(redis_client, user_id):
    if not redis_client:
        return None
    try:
        val = redis_client.get(f"quiz_pending:{user_id}")
        if val is None:
            return None
        return val.decode("utf-8") if hasattr(val, "decode") else str(val)
    except Exception:
        return None


def clear_quiz_pending(redis_client, user_id):
    if not redis_client:
        return
    try:
        redis_client.delete(f"quiz_pending:{user_id}")
    except Exception:
        pass


def set_user_state(redis_client, user_id, state):
    """設定對話狀態：STATE_NORMAL 或 STATE_QUIZ_WAITING。"""
    if not redis_client:
        return
    try:
        redis_client.set(f"user_state:{user_id}", (state or STATE_NORMAL)[:50])
    except Exception:
        pass


def get_user_state(redis_client, user_id):
    if not redis_client:
        return STATE_NORMAL
    try:
        val = redis_client.get(f"user_state:{user_id}")
        if val is None:
            return STATE_NORMAL
        s = val.decode("utf-8") if hasattr(val, "decode") else str(val)
        return s.strip() or STATE_NORMAL
    except Exception:
        return STATE_NORMAL


def set_quiz_data(redis_client, user_id, question, answer_criteria, category):
    """儲存測驗題目與評分標準，供批改使用。"""
    if not redis_client:
        return
    try:
        payload = json.dumps({
            "question": (question or "")[:500],
            "answer_criteria": (answer_criteria or "")[:800],
            "category": (category or "其他")[:30],
        }, ensure_ascii=False)
        redis_client.set(f"quiz_data:{user_id}", payload, ex=3600)
    except Exception:
        pass


def get_quiz_data(redis_client, user_id):
    """取得暫存的測驗資料。回傳 dict 或 None。"""
    if not redis_client:
        return None
    try:
        val = redis_client.get(f"quiz_data:{user_id}")
        if val is None:
            return None
        s = val.decode("utf-8") if hasattr(val, "decode") else str(val)
        return json.loads(s)
    except Exception:
        return None


def clear_quiz_data(redis_client, user_id):
    if not redis_client:
        return
    try:
        redis_client.delete(f"quiz_data:{user_id}")
    except Exception:
        pass


def record_weak_category(redis_client, user_id, category):
    """記錄使用者在某領域表現不佳（測驗答錯時呼叫）。"""
    if not redis_client or not category:
        return
    try:
        key = f"user_weak:{user_id}"
        val = redis_client.hget(key, category)
        count = int(val) if val else 0
        redis_client.hset(key, category, str(count + 1))
    except Exception:
        pass


def get_weak_categories(redis_client, user_id, min_count=2):
    """回傳需加強的領域列表 (category -> count)，只回傳 count >= min_count。"""
    if not redis_client:
        return {}
    try:
        data = redis_client.hgetall(f"user_weak:{user_id}")
        if not data:
            return {}
        out = {}
        for k, v in (data.items() if isinstance(data, dict) else []):
            cat = k.decode("utf-8") if hasattr(k, "decode") else str(k)
            cnt = int(v) if v else 0
            if cnt >= min_count:
                out[cat] = cnt
        return out
    except Exception:
        return {}


def clear_weak_category(redis_client, user_id, category):
    """使用者接受複習筆記後可清除該領域計數。"""
    if not redis_client:
        return
    try:
        redis_client.hdel(f"user_weak:{user_id}", category)
    except Exception:
        pass


def get_last_review_ask(redis_client, user_id):
    """上次詢問「需要複習筆記嗎」的時間戳。"""
    if not redis_client:
        return 0
    try:
        val = redis_client.get(f"last_review_ask:{user_id}")
        if val is None:
            return 0
        return float(val.decode("utf-8") if hasattr(val, "decode") else val)
    except Exception:
        return 0


def set_last_review_ask(redis_client, user_id):
    if not redis_client:
        return
    try:
        redis_client.set(f"last_review_ask:{user_id}", str(time.time()))
    except Exception:
        pass


PENDING_REVIEW_CATEGORY_KEY = "pending_review_category:{user_id}"


def set_pending_review_category(redis_client, user_id, category):
    if not redis_client:
        return
    try:
        redis_client.set(f"pending_review_category:{user_id}", (category or "")[:50])
    except Exception:
        pass


def get_pending_review_category(redis_client, user_id):
    if not redis_client:
        return None
    try:
        val = redis_client.get(f"pending_review_category:{user_id}")
        if val is None:
            return None
        return val.decode("utf-8") if hasattr(val, "decode") else str(val)
    except Exception:
        return None


def clear_pending_review_category(redis_client, user_id):
    if not redis_client:
        return
    try:
        redis_client.delete(f"pending_review_category:{user_id}")
    except Exception:
        pass


def set_last_assistant_message(redis_client, user_id, content):
    """儲存最後一則 assistant 回覆（供動態測驗出題用）。"""
    if not redis_client:
        return
    try:
        redis_client.set(f"last_assistant_message:{user_id}", (content or "").strip()[:2000])
    except Exception:
        pass


def get_last_assistant_message(redis_client, user_id):
    if not redis_client:
        return None
    try:
        val = redis_client.get(f"last_assistant_message:{user_id}")
        if val is None:
            return None
        return val.decode("utf-8") if hasattr(val, "decode") else str(val)
    except Exception:
        return None


def generate_socratic_question(openai_client, last_interaction_context):
    """相容舊 API，改為呼叫 generate_dynamic_quiz。"""
    q, _, _ = generate_dynamic_quiz(openai_client, discussed_topic=None, last_context=last_interaction_context)
    return q


def generate_dynamic_quiz(openai_client, discussed_topic=None, last_context=None, week_topic=None):
    """
    出題邏輯：若 discussed_topic 存在，針對「剛才討論的主題」出開放式簡答題；
    否則依 syllabus_full 本週主題出題。
    回傳 (question_text, answer_criteria, category)。
    """
    # 有剛才討論的主題（使用者問題）→ 針對該主題出題
    if (discussed_topic or "").strip():
        topic_str = discussed_topic.strip()[:200]
        context_str = ""
        if (last_context or "").strip() and len(last_context) > 30:
            context_str = f"\n助教剛才的回答摘要：{last_context[:500]}"
        try:
            resp = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是中醫課程助教。請針對剛才討論的主題出一道「開放式簡答題」。"
                            "回傳 JSON：{\"question\": \"題目\", \"answer_criteria\": \"正確答案要點（供批改用）\", \"category\": \"概念名\"}"
                        ),
                    },
                    {"role": "user", "content": f"剛才討論的主題（使用者問的）：{topic_str}{context_str}\n\n請針對此主題出一道開放式簡答題，回傳 JSON。"},
                ],
                max_tokens=350,
            )
            text = (resp.choices[0].message.content or "").strip()
            for block in (text.split("```"), [text]):
                for raw in block:
                    raw = raw.strip()
                    if raw.startswith("{"):
                        try:
                            obj = json.loads(raw.split("```")[0].strip().split("\n")[0])
                            q = (obj.get("question") or "").strip()[:400]
                            a = (obj.get("answer_criteria") or "").strip()[:600]
                            c = (obj.get("category") or "其他").strip()[:20]
                            if q:
                                return (("小測驗：" + q) if not q.startswith("小測驗") else q, a or q, c or "其他")
                        except Exception:
                            pass
        except Exception:
            traceback.print_exc()

    # 無討論主題 → 依 syllabus_full 本週主題
    try:
        from api.syllabus import get_display_week_lectures
    except ImportError:
        from syllabus import get_display_week_lectures

    display_lec, _, _ = get_display_week_lectures()
    topic = (week_topic or (display_lec and display_lec.get("title")) or "").strip()
    if not topic or topic == "（待填入）":
        topic = "中醫基礎觀念"

    context_hint = ""
    if (last_context or "").strip() and len(last_context) > 50:
        context_hint = f"\n（若與以下近期討論相關可結合出題）近期：{last_context[:400]}"

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是中醫課程助教。根據「本週課程主題」出一道開放式簡答題。"
                        "回傳 JSON：{\"question\": \"題目\", \"answer_criteria\": \"正確答案要點（供批改用）\", \"category\": \"概念名\"}"
                    ),
                },
                {"role": "user", "content": f"本週主題：{topic}{context_hint}\n\n請出一道小測驗，回傳 JSON。"},
            ],
            max_tokens=350,
        )
        text = (resp.choices[0].message.content or "").strip()
        for block in (text.split("```"), [text]):
            for raw in block:
                raw = raw.strip()
                if raw.startswith("{"):
                    try:
                        obj = json.loads(raw.split("```")[0].strip().split("\n")[0])
                        q = (obj.get("question") or "").strip()[:400]
                        a = (obj.get("answer_criteria") or "").strip()[:600]
                        c = (obj.get("category") or "其他").strip()[:20]
                        if q:
                            return (("小測驗：" + q) if not q.startswith("小測驗") else q, a or q, c or "其他")
                    except Exception:
                        pass
    except Exception:
        traceback.print_exc()

    return (f"小測驗：請用 1～2 句話說明本週主題「{topic}」的一個重點。", topic, "其他")


def reveal_quiz_answer(openai_client, question, answer_criteria):
    """使用者點「我不知道」時，公佈答案並給予簡短說明。"""
    if not answer_criteria or not (question or "").strip():
        return "參考課本與講義複習本週重點～"
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "你是中醫助教。根據題目與正確答案要點，用 2～3 句友善說明答案，幫助學生理解。",
                },
                {"role": "user", "content": f"題目：{question[:300]}\n正確答案要點：{answer_criteria[:400]}\n請公佈答案並簡短說明。"},
            ],
            max_tokens=200,
        )
        return (resp.choices[0].message.content or answer_criteria or "").strip()[:500]
    except Exception:
        return (answer_criteria or "參考講義複習～")[:400]


def judge_quiz_answer(openai_client, topic_or_question, student_reply, answer_criteria=None):
    """
    批改測驗回答，回傳 (feedback_text, category_for_weak, was_correct)。
    回饋須包含：稱讚語句、正確性判斷、詳解。
    """
    criteria_ctx = ""
    if answer_criteria:
        criteria_ctx = f"\n正確答案要點（供評分參考）：{answer_criteria[:400]}"
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是中醫助教。批改學生測驗回答，回饋須包含三部分："
                        "1. 稱讚語句（先肯定學生的嘗試）；2. 正確性判斷（對/錯或部分正確）；3. 詳解（說明正確概念或補充要點）。"
                        "回傳 JSON：{\"feedback\": \"完整回饋文字（含稱讚、判斷、詳解）\", \"category\": \"概念名\", \"correct\": true/false}"
                    ),
                },
                {
                    "role": "user",
                    "content": f"題目：{topic_or_question[:250]}{criteria_ctx}\n\n學生回答：{student_reply[:400]}\n\n請批改（含稱讚、判斷、詳解）並回傳 JSON。",
                },
            ],
            max_tokens=350,
        )
        text = (resp.choices[0].message.content or "").strip()
        for block in (text.split("```"), [text]):
            for raw in block:
                raw = raw.strip()
                if raw.startswith("{"):
                    try:
                        obj = json.loads(raw.split("\n")[0].strip())
                        return (
                            (obj.get("feedback") or "謝謝你的回答！").strip()[:600],
                            (obj.get("category") or "其他").strip()[:20],
                            bool(obj.get("correct", True)),
                        )
                    except Exception:
                        pass
        return (text[:400] or "謝謝你的回答！", "其他", True)
    except Exception as e:
        traceback.print_exc()
        return ("謝謝你的回答！", "其他", True)


def generate_review_note(openai_client, category):
    """針對某領域產生簡短複習筆記。"""
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "你是中醫課程助教。針對指定領域，產出一份「簡短複習筆記」（條列重點，約 5～8 點，每點一行），方便學生快速複習。只輸出筆記內容，不要標題以外的多餘說明。",
                },
                {"role": "user", "content": f"領域：{category}\n請產出複習筆記。"},
            ],
            max_tokens=500,
        )
        return (resp.choices[0].message.content or "").strip()[:1500]
    except Exception as e:
        traceback.print_exc()
        return f"【{category}】複習要點請參考課本與講義。"
