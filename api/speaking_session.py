# -*- coding: utf-8 -*-
"""
口說 LIFF 的 Session Manager（規格書第九節）。

負責追蹤「這一場對話」的狀態：目前用的是哪個 Case/Topic、對話進行到第幾輪、
哪些 expected_topics 已經被聊到（用於算 session_state.progress）。

用 Redis 存（key 帶 TTL，長時間沒動作自動過期），因為 Procfile 是
`gunicorn --workers 2`，同一個使用者的請求可能落在不同 worker，純記憶體字典
不會跨 worker 共用。Redis 不可用時 fallback 到 process 內的字典，只保證單一
worker/本機測試可用，正式環境仍應確保 Redis 有連上。

刻意不寫進 MongoDB：跟口說 LIFF 既有的「session 資料用完即丟」原則一致
（見 speaking_liff.py 檔頭說明），這裡只是把原本活在瀏覽器 JS 變數裡的狀態，
搬一部分到後端，讓 hint／session_state 可以依實際病例內容動態產生。
"""
import json
import time
import secrets

SESSION_TTL_SECONDS = 3600  # 1 小時沒有任何一輪對話就視為棄置，自動過期
_REDIS_KEY_PREFIX = "speaking_session:"

_local_store = {}  # redis 不可用時的 fallback（僅限單一 process 內有效）


def _key(session_id):
    return f"{_REDIS_KEY_PREFIX}{session_id}"


def create_session(redis_client, mode, content_id, content_snapshot):
    """
    建立一場新的口說練習 session。
    content_snapshot：抽到的 case 或 topic 完整 dict，整個存進 session，
    之後每輪產生 hint/session_state 不用重新查表，也不受之後內容庫更新影響。
    回傳 session_id（string）。
    """
    session_id = secrets.token_urlsafe(16)
    data = {
        "session_id": session_id,
        "mode": mode,                 # "clinical" | "academic"
        "content_id": content_id,     # case_id 或 topic_id，找不到內容時可能是 None
        "content": content_snapshot,  # case 或 topic 的完整 dict，找不到內容時可能是 None
        "turn_count": 0,
        "covered_topics": [],         # 已經被使用者問到/聊到的 expected_topics 字串
        "created_at": time.time(),
    }
    _save(redis_client, session_id, data)
    return session_id


def get_session(redis_client, session_id):
    """回傳 session dict，不存在或已過期回傳 None。"""
    if not session_id:
        return None
    if redis_client:
        try:
            raw = redis_client.get(_key(session_id))
            if raw is None:
                return None
            raw = raw.decode("utf-8") if hasattr(raw, "decode") else raw
            return json.loads(raw)
        except Exception:
            return None
    entry = _local_store.get(session_id)
    if not entry:
        return None
    if time.time() - entry.get("created_at", 0) > SESSION_TTL_SECONDS:
        _local_store.pop(session_id, None)
        return None
    return entry


def _save(redis_client, session_id, data):
    if redis_client:
        try:
            redis_client.set(_key(session_id), json.dumps(data, ensure_ascii=False), ex=SESSION_TTL_SECONDS)
            return
        except Exception:
            pass
    _local_store[session_id] = data


def record_turn(redis_client, session_id, newly_covered_topics=None):
    """
    每輪對話結束後呼叫：turn_count +1，把這輪新覆蓋到的 expected_topics 併入
    covered_topics（去重）。回傳更新後的 session dict；session 不存在回傳 None。
    """
    session = get_session(redis_client, session_id)
    if not session:
        return None
    session["turn_count"] = session.get("turn_count", 0) + 1
    covered = set(session.get("covered_topics") or [])
    for t in (newly_covered_topics or []):
        t = (t or "").strip()
        if t:
            covered.add(t)
    session["covered_topics"] = list(covered)
    _save(redis_client, session_id, session)
    return session


def compute_session_state(session):
    """
    依 session 目前狀態算出規格書要的 session_state = {phase, progress}。
    progress：已覆蓋的 expected_topics 數 / 該 case/topic 總 expected_topics 數
    （content 缺這個欄位或是 fallback 情境時，用 turn_count 概略估算，避免顯示怪數字）。
    """
    if not session:
        return {"phase": "opening", "progress": 0.0}
    turn_count = session.get("turn_count", 0)
    content = session.get("content") or {}
    expected = content.get("expected_topics") if session.get("mode") == "clinical" else None
    if expected is None:
        expected = content.get("discussion_questions")
    covered = session.get("covered_topics") or []

    if expected:
        progress = min(1.0, len(covered) / max(1, len(expected)))
    else:
        # 沒有 expected_topics 可對照時（例如 fallback 情境），用輪數概略估算，10 輪視為完整
        progress = min(1.0, turn_count / 10)

    if turn_count <= 1:
        phase = "opening"
    elif progress >= 0.8:
        phase = "wrap_up"
    else:
        phase = "in_progress"
    return {"phase": phase, "progress": round(progress, 2)}


def delete_session(redis_client, session_id):
    """對話結束、結算完成後呼叫，主動清除 session（不用等 TTL 過期）。"""
    if not session_id:
        return
    if redis_client:
        try:
            redis_client.delete(_key(session_id))
        except Exception:
            pass
    _local_store.pop(session_id, None)
