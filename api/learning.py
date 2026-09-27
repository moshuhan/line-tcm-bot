# -*- coding: utf-8 -*-
"""
中醫問答的對話狀態輔助：問題記錄、對話歷史（供蘇格拉底式引導判斷「上一輪有沒有
已經引導過」）。

原本這裡還有動態小測驗、弱項追蹤、複習筆記整套「小測驗」功能，已經跟聊天室的
舊小測驗按鈕一起移除。
"""

import json
import time

# Redis key 前綴
QUESTION_LOG_KEY = "question_log"
QUESTION_LOG_MAX = 5000


def log_question(redis_client, user_id, text):
    """將使用者提問記錄到 Redis list，供每週報告使用。輔助功能，失敗不影響主流程。"""
    if not redis_client or not (text or "").strip():
        return
    try:
        payload = json.dumps({"user_id": user_id, "text": (text or "").strip()[:500], "ts": time.time()})
        redis_client.lpush(QUESTION_LOG_KEY, payload)
        redis_client.ltrim(QUESTION_LOG_KEY, 0, QUESTION_LOG_MAX - 1)
    except Exception as e:
        print(f"Redis Log Error: {e}")


def set_last_question(redis_client, user_id, text):
    """儲存最後一則問題。"""
    if not redis_client:
        return
    try:
        redis_client.set(f"last_question:{user_id}", (text or "").strip()[:500])
    except Exception:
        pass


def set_last_assistant_message(redis_client, user_id, content):
    """儲存最後一則 assistant 回覆。"""
    if not redis_client:
        return
    try:
        redis_client.set(f"last_assistant_message:{user_id}", (content or "").strip()[:2000])
    except Exception:
        pass


CONV_HISTORY_KEY = "conv_history:{user_id}"
CONV_HISTORY_MAX_TURNS = 3   # 保留最近 3 輪（user + assistant 各一）
CONV_HISTORY_TTL = 60 * 60   # 1 小時無對話後自動清除


def append_conv_history(redis_client, user_id, user_msg, assistant_msg):
    """將一輪對話（user + assistant）存入 Redis list，超過 MAX_TURNS 自動截頭。"""
    if not redis_client:
        return
    try:
        key = CONV_HISTORY_KEY.format(user_id=user_id)
        turn = json.dumps({"u": (user_msg or "")[:500], "a": (assistant_msg or "")[:800]}, ensure_ascii=False)
        redis_client.rpush(key, turn)
        redis_client.ltrim(key, -CONV_HISTORY_MAX_TURNS, -1)
        redis_client.expire(key, CONV_HISTORY_TTL)
    except Exception:
        pass


def get_conv_history(redis_client, user_id):
    """讀取對話歷史，回傳 list of {u, a}，最舊在前。"""
    if not redis_client:
        return []
    try:
        key = CONV_HISTORY_KEY.format(user_id=user_id)
        items = redis_client.lrange(key, 0, -1) or []
        result = []
        for item in items:
            raw = item.decode("utf-8") if isinstance(item, bytes) else str(item)
            try:
                result.append(json.loads(raw))
            except Exception:
                pass
        return result
    except Exception:
        return []
