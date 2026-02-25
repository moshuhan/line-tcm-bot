# -*- coding: utf-8 -*-
"""
個人化學習分析與互動：問題記錄、蘇格拉底測驗、弱項追蹤、複習筆記。
"""

import json
import time
import traceback
from openai import OpenAI

# Redis key 前綴
QUESTION_LOG_KEY = "question_log"
QUESTION_LOG_MAX = 5000
LAST_QUESTION_KEY = "last_question:{user_id}"
QUIZ_PENDING_KEY = "quiz_pending:{user_id}"
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
    """設定使用者處於「等待測驗回答」狀態。"""
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
    """
    根據 last_interaction_context（最近一筆 assistant 訊息內容）即時生成蘇格拉底式小測驗。
    禁止從靜態題庫抓題，必須以該內容作為 LLM 輸入。
    """
    if not (last_interaction_context or "").strip():
        last_interaction_context = "中醫基礎觀念"
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "你是中醫課程助教，用蘇格拉底提問法出題。根據助教「剛剛」回覆的內容（例如討論了合谷穴），出「一題」啟發式簡答題。例如：「既然我們聊到了合谷穴，你能試著解釋為什麼在某些情況下它被稱為『萬能穴』嗎？」只輸出題目，不要答案。題目以「小測驗：」開頭。",
                },
                {"role": "user", "content": f"助教剛回覆的內容：\n{last_interaction_context[:1500]}\n\n請據此出一題蘇格拉底式小測驗。"},
            ],
            max_tokens=150,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text.startswith("小測驗："):
            text = "小測驗：" + text
        return text[:300]
    except Exception:
        traceback.print_exc()
        return "小測驗：請試著用一句話說明剛才討論的概念。"


def judge_quiz_answer(openai_client, topic_or_question, student_reply):
    """判斷測驗回答，回傳 (feedback_text, category_for_weak, was_correct)。"""
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "你是中醫助教。根據學生對小測驗的回答，給予簡短鼓勵或修正（2～3 句）。並回傳 JSON：{\"feedback\": \"你的回饋文字\", \"category\": \"一個中文概念名，例如：經絡、穴位、辨證\", \"correct\": true 或 false}。",
                },
                {"role": "user", "content": f"題目/主題：{topic_or_question[:200]}\n學生回答：{student_reply[:300]}"},
            ],
            max_tokens=200,
        )
        text = (resp.choices[0].message.content or "").strip()
        for block in (text.split("```"), [text]):
            for raw in block:
                raw = raw.strip()
                if raw.startswith("{"):
                    try:
                        obj = json.loads(raw.split("\n")[0].strip())
                        return (
                            (obj.get("feedback") or "謝謝你的回答！").strip()[:400],
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
