# -*- coding: utf-8 -*-
import glob
import os
import random
import re
import threading
import time
import base64
import json
import secrets
import tempfile
import traceback
from datetime import date, datetime, timezone

# Startup ENV check (names only, no values) for Railway
REDIS_URL = os.getenv("REDIS_URL", "").strip()
print("ENV CHECK: REDIS_URL exists:", bool(REDIS_URL))
print("ENV CHECK: LINE_CHANNEL_ACCESS_TOKEN exists:", bool(os.getenv("LINE_CHANNEL_ACCESS_TOKEN")))
print("ENV CHECK: LINE_CHANNEL_SECRET exists:", bool(os.getenv("LINE_CHANNEL_SECRET")))
print("ENV CHECK: OPENAI_API_KEY exists:", bool(os.getenv("OPENAI_API_KEY")))

from flask import Flask, request, abort, Response, jsonify
import requests
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, PostbackEvent, AudioMessage, ImageMessage,
    QuickReply, QuickReplyButton, MessageAction, FlexSendMessage, URIAction,
)
from redis import Redis as RedisClient
from pymongo import MongoClient
from openai import OpenAI
import httpx
from httpx_retries import RetryTransport, Retry

try:
    from api.syllabus import (
        is_off_topic,
        OFF_TOPIC_REPLY,
    )
    from api.learning import (
        log_question,
        set_last_question,
        set_last_assistant_message,
        append_conv_history,
        get_conv_history,
    )
    from api.research_logging import (
        ensure_user,
        get_interaction_count,
        get_last_interaction_timestamp,
        get_follow_up_count_within_sec,
        classify_qa_intent_and_complexity,
        log_interaction,
        run_analytics_middleware,
    )
except ImportError:
    from syllabus import (
        is_off_topic,
        OFF_TOPIC_REPLY,
    )
    from learning import (
        log_question,
        set_last_question,
        set_last_assistant_message,
        append_conv_history,
        get_conv_history,
    )
    try:
        from research_logging import (
            ensure_user,
            get_interaction_count,
            get_last_interaction_timestamp,
            get_follow_up_count_within_sec,
            classify_qa_intent_and_complexity,
            log_interaction,
            run_analytics_middleware,
        )
    except ImportError:
        def _noop_user(*a, **k):
            return 0
        def _noop_ts(*a, **k):
            return None
        def _noop_classify(*a, **k):
            return (None, None)
        ensure_user = get_interaction_count = get_follow_up_count_within_sec = _noop_user
        get_last_interaction_timestamp = _noop_ts
        classify_qa_intent_and_complexity = _noop_classify
        log_interaction = lambda *a, **k: None
        run_analytics_middleware = lambda *a, **k: None
        append_conv_history = lambda *a, **k: None
        get_conv_history = lambda *a, **k: []

try:
    from api.exam_quiz import (
        list_categories as exam_list_categories,
        get_questions_by_category as exam_get_questions_by_category,
        list_exam_periods as exam_list_exam_periods,
        get_questions_by_period as exam_get_questions_by_period,
        get_question_detail as exam_get_question_detail,
        submit_exam as exam_submit_exam,
        toggle_bookmark as exam_toggle_bookmark,
        list_wrong_questions as exam_list_wrong_questions,
        remove_wrong_question as exam_remove_wrong_question,
    )
    from api.liff_auth import verify_liff_id_token
    from api.speaking_liff import (
        list_modes as speaking_list_modes,
        list_cases as speaking_list_cases,
        mint_ephemeral_session as speaking_mint_ephemeral_session,
        process_turn as speaking_process_turn,
        build_session_summary as speaking_build_session_summary,
        end_session as speaking_end_session,
    )
    from api.writing_liff import (
        load_topics as writing_load_topics,
        annotate_realtime as writing_annotate_realtime,
        full_review as writing_full_review,
        save_practice as writing_save_practice,
        list_practice as writing_list_practice,
        discard_practice as writing_discard_practice,
    )
except ImportError:
    from exam_quiz import (
        list_categories as exam_list_categories,
        get_questions_by_category as exam_get_questions_by_category,
        list_exam_periods as exam_list_exam_periods,
        get_questions_by_period as exam_get_questions_by_period,
        get_question_detail as exam_get_question_detail,
        submit_exam as exam_submit_exam,
        toggle_bookmark as exam_toggle_bookmark,
        list_wrong_questions as exam_list_wrong_questions,
        remove_wrong_question as exam_remove_wrong_question,
    )
    from liff_auth import verify_liff_id_token
    from speaking_liff import (
        list_modes as speaking_list_modes,
        list_cases as speaking_list_cases,
        mint_ephemeral_session as speaking_mint_ephemeral_session,
        process_turn as speaking_process_turn,
        build_session_summary as speaking_build_session_summary,
        end_session as speaking_end_session,
    )
    from writing_liff import (
        load_topics as writing_load_topics,
        annotate_realtime as writing_annotate_realtime,
        full_review as writing_full_review,
        save_practice as writing_save_practice,
        list_practice as writing_list_practice,
        discard_practice as writing_discard_practice,
    )

# 1. 初始化
app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
line_webhook_handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
# 使用 httpx + RetryTransport 緩解連線瞬斷
_retry = Retry(total=3, backoff_factor=0.5)
_http_client = httpx.Client(
    transport=RetryTransport(retry=_retry),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=20),
)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), http_client=_http_client)

# Redis：Railway 使用 REDIS_URL，標準 redis-py 連線（decode_responses=True 回傳 str）
redis = None
if REDIS_URL:
    try:
        redis = RedisClient.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=5,
        )
        redis.ping()
        print(">>> SUCCESS: Connected to Railway Redis via REDIS_URL <<<")
    except Exception as e:
        print(f">>> ERROR: Failed to connect to Redis: {e} <<<")
        redis = None

# MongoDB：Railway 使用 MONGO_URL，標準 pymongo 連線（嚴格避免默認連到 localhost）
MONGO_URL = os.getenv("MONGO_URL", "").strip()
print(f">>> BOOT: Loading MONGO_URL (length: {len(MONGO_URL)})")

mongo_client = None
mongo_db = None
if not MONGO_URL:
    print(">>> CRITICAL ERROR: MONGO_URL is empty! Check Railway Variables. <<<")
else:
    try:
        # 明確指定 URI 與連線 timeout，避免使用預設 localhost:27017
        mongo_client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")
        mongo_db = mongo_client.get_database("line-tcm-bot")
        print(f">>> BOOT SUCCESS: MongoDB is ready! db={getattr(mongo_db, 'name', None)} <<<")
        # 讓 Compass 直接看到 collection（第一次寫入也會自動建立；這裡只是加速可見性）
        try:
            existing = set(mongo_db.list_collection_names())
            if "StudentFeedback" not in existing:
                mongo_db.create_collection("StudentFeedback")
                print(">>> BOOT: created collection StudentFeedback <<<")
        except Exception as e:
            print(f">>> BOOT: create_collection StudentFeedback skipped err={e}")
    except Exception as e:
        print(f">>> BOOT ERROR: MongoDB connection failed: {e}")
        mongo_client = None
        mongo_db = None

# 安全聲明：涉及中醫診斷之回覆必須附加（詳細回答 + 參考出處後加此句）
SAFETY_DISCLAIMER = "\n\n以上資料僅供參考，若有身體不適請務必尋求專業醫師診斷與建議。"
SAFETY_DISCLAIMER_EN = "\n\nThe above information is for reference only. Please seek professional medical advice if you have any health concerns."

USER_LANGUAGE_KEY = "user_language:{user_id}"

TIMEOUT_SECONDS = 28  # Assistant + RAG 常需 15–30 秒；保留 buffer 避開 Vercel 預設 30s
TIMEOUT_MESSAGE = "正在努力翻閱典籍/資料中，請稍候再問我一次。"
FORCE_PUSH_MODE = os.getenv("LINE_FORCE_PUSH", "true").strip().lower() in ("1", "true", "yes", "on")
# 英文版部署時設 FORCE_LANG=en，強制所有回覆使用英文，不依賴動態語言偵測
FORCE_LANG = os.getenv("FORCE_LANG", "").strip().lower()  # "en" | "" (空=動態偵測)

# 「智慧問答」路徑共用的模型常數：聊天室中醫問答（_tcm_openai_reply）跟國考題庫
# 「AI 即刻問／查看完整詳解」（_exam_explain_answer／_exam_explain_structured）都吃這個常數，
# 只改這裡就能一次換掉三個地方的模型，不用到處找。
# gpt-5 系列（含 gpt-5.4）不支援自訂 temperature（只能用預設值），且用 max_completion_tokens
# 取代 max_tokens——這三個呼叫點已經配合改好，若之後要換回 gpt-4o 系列，記得把這兩個參數
# 換回來（gpt-4o 系列吃 max_tokens，且支援自訂 temperature，拿掉 temperature 只是少了那個
# 0.2 的低隨機性調校，不會報錯，但建議換回來維持原本的穩定輸出風格）。
_SMART_QA_MODEL = "gpt-5.4"

# --- QuickReply ---
# 聊天室現在只有中醫問答一種功能，不需要模式切換按鈕，統一不附加 quick reply。
def text_with_quick_reply(content):
    return TextSendMessage(text=content)

# --- 中醫問答：tcm_master_knowledge.json + OpenAI gpt-4o-mini（純 OpenAI）---
try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_TCM_JSON_CACHE = None
_TCM_FULL_CONTEXT_CACHE = None
_TCM_EMBEDDINGS_CACHE = None  # list[{"category", "text", "embedding"}]

def _load_tcm_json():
    """載入 data/tcm_master_knowledge.json，快取。"""
    global _TCM_JSON_CACHE
    if _TCM_JSON_CACHE is not None:
        return _TCM_JSON_CACHE
    paths = glob.glob(os.path.join(_DATA_DIR, "tcm_*.json"))
    out = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
                if d and isinstance(d, dict):
                    out.append(d)
        except Exception:
            pass
    _TCM_JSON_CACHE = out
    return _TCM_JSON_CACHE

def _build_full_tcm_context():
    """將 tcm_master_knowledge.json 全部知識點序列化為文字 context（快取）。"""
    global _TCM_FULL_CONTEXT_CACHE
    if _TCM_FULL_CONTEXT_CACHE is not None:
        return _TCM_FULL_CONTEXT_CACHE
    parts = []
    for data in _load_tcm_json():
        for kp in data.get("knowledge_points") or []:
            block = []
            if kp.get("category"):
                block.append(f"【{kp['category']}】")
            if kp.get("core_logic"):
                block.append(kp["core_logic"])
            if kp.get("mechanism"):
                block.append(kp["mechanism"])
            for cr in (kp.get("causal_relationships") or []):
                if isinstance(cr, dict):
                    block.append(f"{cr.get('emotion','')}→{cr.get('impact','')}：{cr.get('symptoms','')}")
            for pf in (kp.get("pathological_features") or []):
                if isinstance(pf, dict):
                    block.append(f"{pf.get('evil','')}：{pf.get('features','')}")
            for row in (kp.get("five_elements_table") or []):
                if isinstance(row, dict):
                    block.append(json.dumps(row, ensure_ascii=False))
            for qa in (kp.get("student_qa") or []):
                if isinstance(qa, str):
                    block.append(qa)
            if kp.get("interactions"):
                for k, v in (kp["interactions"] or {}).items():
                    block.append(f"{k}: {v}")
            for ii in (kp.get("inspection_items") or []):
                if isinstance(ii, dict):
                    block.append(ii.get("item", "") + ": " + (ii.get("logic") or ", ".join(ii.get("types", []))))
            if kp.get("mapping"):
                for k, v in (kp["mapping"] or {}).items():
                    block.append(f"{k}: {v}")
            for feat in (kp.get("features") or []):
                if isinstance(feat, dict):
                    block.append(feat.get("type", "") + ": " + (feat.get("logic") or ""))
                    for d in (feat.get("details") or []):
                        if isinstance(d, dict):
                            block.append(json.dumps(d, ensure_ascii=False))
            for item in (kp.get("items") or []):
                if isinstance(item, dict):
                    block.append(f"{item.get('name','')}: {item.get('logic','')}")
            for d in (kp.get("details") or []):
                if isinstance(d, dict):
                    label = d.get("type") or d.get("item", "")
                    block.append(f"{label}: {d.get('logic','')}")
            for t in (kp.get("types") or []):
                if isinstance(t, dict):
                    block.append(f"{t.get('name','')}: {t.get('logic','')}")
            if kp.get("functions"):
                block.append(kp["functions"])
            for m in (kp.get("methods") or []):
                if isinstance(m, dict):
                    block.append(f"{m.get('name','')}: {m.get('details','')}")
            for cc in (kp.get("common_conditions") or []):
                if isinstance(cc, str):
                    block.append(cc)
            for tq in (kp.get("ten_questions_logic") or []):
                if isinstance(tq, dict):
                    block.append(f"{tq.get('item','')}: {tq.get('logic','')}")
            if kp.get("pulse_mapping"):
                for k, v in (kp["pulse_mapping"] or {}).items():
                    block.append(f"{k}: {v}")
            for cp in (kp.get("common_pulses") or []):
                if isinstance(cp, dict):
                    block.append(f"{cp.get('pulse','')}: {cp.get('logic','')}")
            if block:
                parts.append("\n".join(block))
    result = "\n\n".join(parts) if parts else ""
    _TCM_FULL_CONTEXT_CACHE = result
    return result


def _load_tcm_embeddings():
    """
    載入 data/tcm_embeddings.json（由 scripts/generate_embeddings.py 產生），快取至記憶體。
    回傳 list[dict]，每筆含 "category"、"text"、"embedding"（list[float]）。
    檔案不存在時回傳空 list（自動 fallback 到關鍵字模式）。
    """
    global _TCM_EMBEDDINGS_CACHE
    if _TCM_EMBEDDINGS_CACHE is not None:
        return _TCM_EMBEDDINGS_CACHE
    path = os.path.join(_DATA_DIR, "tcm_embeddings.json")
    if not os.path.isfile(path):
        _TCM_EMBEDDINGS_CACHE = []
        return _TCM_EMBEDDINGS_CACHE
    try:
        with open(path, "r", encoding="utf-8") as f:
            _TCM_EMBEDDINGS_CACHE = json.load(f)
        print(f"[Embed] 載入 {len(_TCM_EMBEDDINGS_CACHE)} 筆 knowledge_point embeddings")
    except Exception as e:
        print(f"[Embed] 載入失敗：{e}")
        _TCM_EMBEDDINGS_CACHE = []
    return _TCM_EMBEDDINGS_CACHE


def _semantic_search(query_text: str, top_k: int = 3) -> str:
    """
    語義向量搜尋：embed 使用者問題 → cosine similarity → 回傳 Top-K 知識點的合併文字。
    numpy 不可用、embeddings 未載入、或 API 失敗時回傳空字串，讓呼叫端 fallback。
    """
    if not _NUMPY_AVAILABLE:
        return ""
    records = _load_tcm_embeddings()
    if not records:
        return ""
    try:
        resp = client.embeddings.create(model="text-embedding-3-small", input=query_text[:2000])
        q_vec = np.array(resp.data[0].embedding, dtype="float32")
    except Exception as e:
        print(f"[Embed] query embedding 失敗：{e}")
        return ""

    scores = []
    for rec in records:
        try:
            kp_vec = np.array(rec["embedding"], dtype="float32")
            sim = float(np.dot(q_vec, kp_vec) / (np.linalg.norm(q_vec) * np.linalg.norm(kp_vec) + 1e-9))
            scores.append((sim, rec["text"]))
        except Exception:
            continue

    scores.sort(key=lambda x: x[0], reverse=True)
    return "\n\n".join(text for _, text in scores[:top_k])


# --- 中醫問答：近十年國考題庫語意檢索（讓聊天室回答優先參考真實考題與官方正解）---
_EXAM_EMBED_CACHE = None  # {"matrix": np.ndarray (N,512), "norms": np.ndarray (N,), "ids": list[str]}；{} 表示不可用
_EXAM_QUESTIONS_BY_ID = None  # dict[id -> question dict]，快取
_EXAM_SIM_THRESHOLD = 0.25  # 相似度低於這個門檻就不附加，避免不相關的題目誤導模型


def _load_exam_embeddings():
    """
    載入 data/exam_embeddings.npy + exam_embeddings_ids.json（由
    scripts/generate_exam_embeddings.py 產生，5254 題國考題庫的 embedding），快取至記憶體。
    檔案不存在（還沒跑過產生腳本）或 numpy 不可用時回傳空 dict，讓呼叫端 fallback。
    """
    global _EXAM_EMBED_CACHE
    if _EXAM_EMBED_CACHE is not None:
        return _EXAM_EMBED_CACHE
    _EXAM_EMBED_CACHE = {}
    if not _NUMPY_AVAILABLE:
        return _EXAM_EMBED_CACHE
    npy_path = os.path.join(_DATA_DIR, "exam_embeddings.npy")
    ids_path = os.path.join(_DATA_DIR, "exam_embeddings_ids.json")
    if not (os.path.isfile(npy_path) and os.path.isfile(ids_path)):
        return _EXAM_EMBED_CACHE
    try:
        matrix = np.load(npy_path)
        with open(ids_path, "r", encoding="utf-8") as f:
            ids = json.load(f)
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1e-9
        _EXAM_EMBED_CACHE = {"matrix": matrix, "norms": norms, "ids": ids}
        print(f"[ExamEmbed] 載入 {len(ids)} 題國考題庫 embeddings，shape={matrix.shape}")
    except Exception as e:
        print(f"[ExamEmbed] 載入失敗：{e}")
        _EXAM_EMBED_CACHE = {}
    return _EXAM_EMBED_CACHE


def _load_exam_questions_by_id():
    """載入 data/exam_questions.json，依 id 建立查詢字典，快取至記憶體。"""
    global _EXAM_QUESTIONS_BY_ID
    if _EXAM_QUESTIONS_BY_ID is not None:
        return _EXAM_QUESTIONS_BY_ID
    _EXAM_QUESTIONS_BY_ID = {}
    path = os.path.join(_DATA_DIR, "exam_questions.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            questions = json.load(f)
        for q in questions:
            if q.get("id"):
                _EXAM_QUESTIONS_BY_ID[q["id"]] = q
    except Exception as e:
        print(f"[ExamEmbed] 載入 exam_questions.json 失敗：{e}")
    return _EXAM_QUESTIONS_BY_ID


def _format_exam_question_for_context(q):
    lines = [f"【題目】{q.get('question', '')}"]
    options = q.get("options") or {}
    for letter in ("A", "B", "C", "D"):
        if options.get(letter):
            lines.append(f"{letter}. {options[letter]}")
    if q.get("answer"):
        lines.append(f"【正確答案】{q['answer']}")
    return "\n".join(lines)


def _semantic_search_exam(query_text: str, top_k: int = 3) -> str:
    """
    在近十年中醫國考題庫（5254 題）裡做語意向量搜尋，回傳最相關 Top-K 題的題目、選項、
    官方正解，格式化成文字 context。找不到相關題目、embeddings 未產生、或 API 失敗時
    回傳空字串，讓呼叫端 fallback（不影響原本教材知識庫的問答流程）。
    """
    cache = _load_exam_embeddings()
    if not cache:
        return ""
    try:
        resp = client.embeddings.create(
            model="text-embedding-3-small", input=query_text[:2000], dimensions=512
        )
        q_vec = np.array(resp.data[0].embedding, dtype="float32")
    except Exception as e:
        print(f"[ExamEmbed] query embedding 失敗：{e}")
        return ""

    matrix, norms, ids = cache["matrix"], cache["norms"], cache["ids"]
    q_norm = float(np.linalg.norm(q_vec)) + 1e-9
    sims = matrix.dot(q_vec) / (norms * q_norm)
    top_idx = np.argsort(sims)[::-1][:top_k]

    by_id = _load_exam_questions_by_id()
    parts = []
    for i in top_idx:
        if sims[i] < _EXAM_SIM_THRESHOLD:
            continue
        q = by_id.get(ids[i])
        if q:
            parts.append(_format_exam_question_for_context(q))
    return "\n\n".join(parts)


def _build_exam_grounded_context(query_text: str) -> str:
    """
    統一的『國考題庫優先、教材補充』context 組裝邏輯：聊天室中醫問答、國考題「AI 即刻問」
    與「查看完整詳解」三個路徑都共用這個函式，確保檢索優先順序一致——都是先查近十年
    國考題庫，再查教材知識庫補充，兩邊都查不到才 fallback 到全量教材文字。
    """
    exam_ctx = _semantic_search_exam(query_text, top_k=3)
    kb_ctx = _semantic_search(query_text, top_k=2)
    parts = []
    if exam_ctx:
        parts.append("【近十年中醫國考題庫參考】\n" + exam_ctx)
    if kb_ctx:
        parts.append("【教材補充資料】\n" + kb_ctx)
    ctx = "\n\n".join(parts)
    return ctx if ctx.strip() else _build_full_tcm_context()[:4000]


_TCM_SYSTEM_PROMPT = """
【最優先規則：在閱讀任何對話歷史之前，先判斷使用者「最新這一則」訊息的意圖】
- 若是社交短句（謝謝、好的、了解、再見、哈囉、讚、收到、沒問題等），只需一句話親切回應，不附任何中醫內容或資料來源。
- 若是詢問課程聯絡方式、助教或老師資訊，只需回覆：「相關問題請至課程 LINE 群組發問。」，不需其他內容。
- 其他問題才依照以下中醫學術助教的原則完整回答。

你是中醫學術助教，回答中醫專業問題時請遵循以下原則：

【蘇格拉底式引導原則——這是核心教學策略，判斷順序在內容/格式規則之前】
1. 先判斷使用者「最新這一則」訊息跟前面對話歷史的關聯度：如果話題明顯換了（新主題、新病證、新方劑、新經典段落等，跟前面問的關聯度低），一律視為全新問題，不要勉強把它套進舊主題的脈絡，直接針對這個新問題重新開始引導。
2. 面對一個「還沒引導過」的新問題，不要一次就把完整答案倒給學生。先用 1-2 句話，針對這題最關鍵的切入點，提出一個引導性的反問或提示（例如點出該考慮哪個辨證要素、該回想哪一段經典原文、該比較哪兩個容易混淆的概念），幫助學生自己想到方向。
3. 引導必須「收斂」——牢牢扣著使用者問的這個問題本身，不要為了引導而扯出不相關的延伸主題、反問或知識點，把話題越帶越開。
4. 引導最多只進行一輪。只要符合下列任一情況，這一輪「必須」完整公布答案，並依照下面的【格式規則】簡潔說明——不可以再提出下一個引導反問、不可以再問「那你覺得該用哪個方劑」這類延伸問題：
   (a) 對話歷史裡，你上一則回覆已經對「同一個問題」給過引導（不管學生這次回得對不對、完不完整）；或
   (b) 學生明確表示不知道、答不出來、或直接要求公布答案——包含完整句子（如「告訴我答案」「不要引導了」），也包含極簡短的要求（如只打一個字「答」，或「答案」「解答」）。
   換句話說：同一個問題最多只能引導一次，第二輪一定要收斂給出完整答案，不能一直用新的反問把答案往後拖。
5. 純社交短句、課程行政問題不套用引導，維持最上面【最優先規則】的處理方式。

【內容原則】
0. 若下方背景資料包含「近十年中醫國考題庫參考」區塊：這是近十年（105～115年）中醫師執照考試的真實考題與官方正解，優先度高於你自己的中醫知識——你的說明邏輯、辨證方向、最終結論都必須跟這裡列出的正確答案一致，不要給出跟題庫正解矛盾的說法。可以用自己的知識補充解釋「為什麼」，但不能推翻題庫給的答案。若題庫參考跟這一題關聯度不高（只是語意相近但問的不是同一件事），就當作沒有這個區塊，依下列原則正常作答。
1. 優先從「課程教材」、「中醫經典文獻（如：黃帝內經、傷寒雜病論、神農本草經）」以及「PubMed 上的現代醫學論文」中提取資訊。
2. 嚴禁自行推斷或編造未經證實的療效。若資料庫中無相關記載，請誠實告知。
3. 避免產生幻覺，不確定的資訊不要提供。
4. 若使用者提出與中醫無直接關聯的一般性問題（如飲食、生活習慣），可簡短從中醫養生角度給一句建議，再邀請繼續提問。

【格式規則——適用於「公布答案」的回合；引導反問的回合請維持 1-2 句話、不要用條列格式】
- 公布答案時控制在 3-5 個重點以內，每點不超過 2 句話。
- 禁止開場白（如「很高興為您解答」「這是一個很好的問題」），直接進入內容。
- 不要為了顯得親切而重複解釋、加註安慰語句，或延伸沒被問到的內容。

【語氣——靠用詞體現，不是靠篇幅】
- 避免生硬的醫學術語堆疊，適度使用「你可以想成……」「簡單說……」這類引導語幫助理解。
- 語氣保持專業、客觀即可，不需要額外句子表現熱情或親切。

【資料來源——依回答深淺選擇性顯示】
- 簡短回答（3 個重點以內、非深入辨證或處方類問題）：文末只標註一個關鍵字來源即可，例如「資料來源：黃帝內經」。
- 深入回答（複雜辨證、方劑組成、臨床機轉等）：文末列出完整「資料來源：」，包含書名、章節或論文標題。
- 兩種情況都必須有「資料來源：」這一行，只是詳細程度不同。
""".strip()

_TCM_SYSTEM_PROMPT_EN = """
[TOP PRIORITY — evaluate the user's LATEST message ONLY, ignoring prior conversation:]
- If it is a social phrase (thanks, ok, great, bye, hello, got it, noted, etc.), reply in ONE warm sentence only — no TCM content, no sources.
- If it is asking for course contact info, the TA, or the instructor, reply ONLY with: "Please ask in the course LINE group." — nothing more.
- For all other messages, follow the full TCM guidelines below.

You are a TCM (Traditional Chinese Medicine) academic assistant. When answering TCM questions, follow these principles:

[Socratic guidance principle — this is the core teaching strategy, applied before the content/format rules below]
1. First judge how related the user's LATEST message is to the recent conversation history: if the topic has clearly changed (a new subject, pattern, formula, classical passage, etc. with low relevance to what came before), treat it as a brand-new question — don't force it into the old topic's context; restart guidance on the new question directly.
2. For a new question you haven't guided on yet, do NOT dump the full answer immediately. First give a short (1-2 sentence) guiding question or hint focused on the single most important entry point (e.g. which pattern-differentiation factor to consider, which classical passage to recall, which two easily-confused concepts to compare) — help the student think their own way toward it.
3. Guidance must stay converged on the actual question asked — don't wander into unrelated tangents, side-questions, or extra concepts just to "guide more."
4. Guidance runs for at most ONE round. As soon as either condition below is met, you MUST reveal the full answer this turn, formatted per the [Format rules] below — do NOT ask yet another guiding question or probe deeper (e.g. "so which formula would you use for that?"):
   (a) your previous reply already gave a guiding hint on this same question (regardless of whether the student's attempt was right, wrong, or partial), or
   (b) the student explicitly asks for the answer or to stop guiding — whether in a full sentence ("just tell me the answer", "stop guiding") or a terse one-word request ("answer", "ans").
   In other words: a given question gets guided at most once — the second round must converge on the complete answer, not stall with another guiding question.
5. Social phrases and course-administration questions skip this guidance entirely — handle them per the TOP PRIORITY rule above.

[Content principles]
0. If the context below includes a "Reference: Last-10-Years TCM Licensing Exam Questions" block: these are real questions and official correct answers from Taiwan's TCM licensing exam (years 105-115). This takes priority over your own knowledge — your reasoning, pattern-differentiation direction, and final conclusion must be consistent with the correct answer(s) shown there; do not contradict them. You may use your own knowledge to explain "why," but never override the exam's given answer. If the retrieved exam questions are only loosely/semantically related and not actually about the same question, ignore this block and answer normally per the principles below.
1. Prioritize information from course materials, TCM classical texts (e.g., Huangdi Neijing, Shang Han Lun, Shen Nong Ben Cao Jing), and modern medical papers on PubMed.
2. Never fabricate or infer unverified therapeutic effects. If the information is not in the knowledge base, say so honestly.
3. Avoid hallucinations — do not provide information you are uncertain about.
4. If the user asks a general question not directly related to TCM (e.g., food, lifestyle), give one brief suggestion from a TCM wellness perspective, then invite further questions.

[Format rules — apply to the "reveal the answer" turn; a guiding-question turn should stay 1-2 plain sentences, no bullet list]
- When revealing the answer, keep it to 3-5 key points at most, no more than 2 sentences per point.
- No opening filler (e.g. "Great question!", "I'm happy to help") — go straight into the content.
- Do not repeat yourself, add reassuring filler, or expand into anything not actually asked, just to seem warm.

[Tone — through word choice, not through length]
- Avoid stacking dense medical jargon; use light guiding phrases like "think of it as..." or "in short..." where helpful.
- Stay professional and objective — no extra sentences needed to signal warmth or enthusiasm.

[Sources — selective, based on answer depth]
- Short answers (3 points or fewer, not a deep pattern-differentiation/prescription question): end with just one keyword source, e.g. "Sources: Huangdi Neijing".
- In-depth answers (complex pattern differentiation, formula composition, clinical mechanism): end with a full "Sources:" line including book title, chapter, or paper title.
- Either way, always include a "Sources:" line — only the level of detail differs.
- Respond entirely in English.
""".strip()


def _ensure_sources_section(text: str, english: bool = False) -> str:
    """確保回覆末尾包含來源區段（保底防漏）。"""
    t = (text or "").strip()
    if not t:
        return t
    if english:
        if "Source:" in t or "Sources:" in t:
            return t
        return t + "\n\nSources: none (not in knowledge base)"
    else:
        if "資料來源：" in t or "Source:" in t or "Sources:" in t:
            return t
        return t + "\n\n資料來源：無（資料庫未收錄/不足以支持）"


def _is_english_input(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    has_latin = bool(re.search(r"[A-Za-z]", t))
    has_cjk = bool(re.search(r"[\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF]", t))
    return has_latin and not has_cjk


def _set_user_language(user_id, lang):
    if not redis or not user_id or not lang:
        return
    try:
        redis.set(USER_LANGUAGE_KEY.format(user_id=user_id), lang, ex=7 * 24 * 3600)
    except Exception:
        pass


def _get_user_language(user_id):
    if not redis or not user_id:
        return "zh"
    try:
        val = redis.get(USER_LANGUAGE_KEY.format(user_id=user_id))
        if val is None:
            return "zh"
        if isinstance(val, bytes):
            val = val.decode("utf-8", errors="replace")
        return str(val or "zh").strip().lower() or "zh"
    except Exception:
        return "zh"


# 模組載入時預熱 TCM 快取，減少首次問答延遲
try:
    _load_tcm_json()
    _build_full_tcm_context()
except Exception:
    pass


def _start_loading_indicator(user_id, loading_seconds=20):
    """
    呼叫 LINE Chat Loading API，在聊天室顯示打字動畫（三個點）。
    不消耗 push 額度，動畫最長持續 loading_seconds 秒後自動消失。
    失敗不影響主流程。
    """
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    if not token or not user_id:
        return
    try:
        requests.post(
            "https://api.line.me/v2/bot/chat/loading/start",
            json={"chatId": user_id, "loadingSeconds": loading_seconds},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=3,
        )
    except Exception as e:
        print(f"[LOADING] failed err={e}")

def _log_interaction_to_mongodb_async(user_id, text, ai_reply, is_eng):
    """
    背景非同步記錄：chat_history + interaction + research logging。
    避免阻塞主流程。
    """
    if mongo_db is None:
        print(">>> LOGGING ERROR: db instance is None, skipping async logging")
        return
    try:
        # 取得 LINE 使用者名稱
        user_name = None
        try:
            prof = line_bot_api.get_profile(user_id)
            user_name = getattr(prof, "display_name", None)
        except Exception:
            user_name = None

        # 寫入 chat_history
        mongo_db.chat_history.insert_one(
            {
                "user_id": (user_name or "").strip()[:200] or user_id,
                "userId": user_id,
                "userName": (user_name or "").strip()[:200] or None,
                "question": text,
                "answer": ai_reply,
                "timestamp": datetime.now(timezone.utc),
                "source": "unified_loop",
            }
        )
        print(f">>> MONGODB: Successfully logged message from {user_id}")

        # 寫入研究資料
        try:
            ensure_user(mongo_db, user_id)
            count_before = get_interaction_count(mongo_db, user_id)
            last_ts = get_last_interaction_timestamp(mongo_db, user_id)
            now_utc = datetime.now(timezone.utc)
            session_duration_sec = (now_utc - last_ts).total_seconds() if last_ts else 0
            follow_up = get_follow_up_count_within_sec(mongo_db, user_id, within_sec=1800)
            intent_tag, complexity_score = classify_qa_intent_and_complexity(client, text)
            log_interaction(
                mongo_db,
                user_id,
                "QA",
                text,
                ai_reply,
                intent_tag=intent_tag,
                complexity_score=complexity_score,
                session_duration_sec=session_duration_sec,
                follow_up_count=follow_up,
                feedback_requested=((count_before + 1) % 20 == 0),
            )
            run_analytics_middleware(mongo_db, user_id)
        except Exception as e:
            print(f">>> RESEARCH LOGGING ERROR: {e}")
    except Exception as e:
        print(f">>> MONGODB ERROR: Failed to log message: {e}")


def _tcm_openai_reply(user_id, text, reply_token=None):
    """
    以「近十年中醫國考題庫」為優先 context、tcm_master_knowledge.json 為補充 context，
    用 OpenAI 生成回覆。語義向量搜尋（_semantic_search_exam／_semantic_search）分別找出
    最相關的國考題目與教材知識點；兩邊都找不到時 fallback 至全量 context。不經過 Assistant API。
    回傳 True 若已回覆，False 若失敗。
    """
    if not (text or "").strip():
        return False
    import time

    txt = text.strip()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return False
    start_ts = time.time()

    # 學生想跳過引導、直接要答案時的簡短暗號——不只靠模型自己判斷語意，用程式碼明確
    # 攔截幾個常見的極簡短寫法，確保「打『答』就直接公布答案」100% 生效，不會因為
    # 訊息太短、模型誤判成別的意思而繼續引導。
    _direct_answer_zh = {"答", "答案", "解答", "公布答案", "給答案", "直接給答案", "直接公布答案", "揭曉答案"}
    _direct_answer_en = {"answer", "ans", "reveal", "reveal the answer"}
    is_direct_answer_request = txt in _direct_answer_zh or txt.lower() in _direct_answer_en

    # 帶入最近 3 輪對話歷史，讓 GPT 自己判斷這一題是新問題還是同一題的延續
    # （是否換題交給 system prompt 的蘇格拉底式引導原則判斷，這裡不用「有沒有歷史」
    # 這種粗略條件強制套格式）。但「上一輪是引導還是已經公布答案」這件事不能只靠
    # 模型自己記憶多輪歷史來判斷——實測會一直順著引導問下去、忘記已經引導過一次了，
    # 所以這裡改成用程式判斷：上一輪回覆有沒有「資料來源／Sources」這個公布答案時
    # 一定會有的標記，來明確告訴模型現在是不是已經引導過一次。
    history = get_conv_history(redis, user_id)

    # 如果這則訊息本身就是「答」這種簡短暗號，它本身沒有實質內容可以拿去檢索——
    # 真正的問題在上一輪的學生提問裡，用那個去查題庫／教材，而不是拿「答」這個字去查
    # （用「答」查語意檢索只會查到不相關的內容）。
    retrieval_query = txt
    if is_direct_answer_request and history:
        retrieval_query = history[-1].get("u", "") or txt

    # 優先查國考題庫（近十年真實考題＋官方正解），再查教材知識庫補充；
    # 兩邊都查不到才 fallback 到全量教材文字（維持原本行為）
    ctx = _build_exam_grounded_context(retrieval_query)

    if not ctx or not ctx.strip():
        return False
    try:
        is_eng = True if FORCE_LANG == "en" else _is_english_input(retrieval_query)
        # 顯示給模型看的「這一題在問什麼」：暗號訊息就顯示回原本的問題，而不是顯示「答」
        # 這個字本身——這樣模型才知道要公布的是哪一題的答案。
        question_for_prompt = retrieval_query if (is_direct_answer_request and history) else txt
        if is_eng:
            system_prompt = _TCM_SYSTEM_PROMPT_EN
            disclaimer = SAFETY_DISCLAIMER_EN
            user_question = (
                f"[Context]\n{ctx}\n\n[Question]\n{question_for_prompt}\n\n"
                f"IMPORTANT: If the question above is a social phrase (e.g. 'thank you', 'ok', 'great', 'bye', 'got it'), "
                f"reply in ONE short sentence only — no TCM content, no sources, no key points.\n"
                f"Otherwise, follow the Socratic guidance principle in the system prompt to decide whether to "
                f"guide first or reveal the answer now — do not skip the guidance step and jump straight to the answer."
            )
        else:
            system_prompt = _TCM_SYSTEM_PROMPT
            disclaimer = SAFETY_DISCLAIMER
            user_question = (
                f"[背景資料]\n{ctx}\n\n[問題]\n{question_for_prompt}\n\n"
                f"重要：若上方問題是社交短句（如「謝謝」「好的」「了解」「再見」等），只需一句話親切回應，不附任何中醫內容或資料來源。\n"
                f"否則請依照 system prompt 的蘇格拉底式引導原則，判斷這一題該先引導還是直接公布答案，"
                f"不要跳過引導步驟直接給答案。"
            )

        messages = [{"role": "system", "content": system_prompt}]
        for turn in history:
            messages.append({"role": "user", "content": turn.get("u", "")})
            messages.append({"role": "assistant", "content": turn.get("a", "")})

        # already_revealed：上一輪是不是已經公布過答案了（沒有歷史就當作 None，不適用）。
        # should_reveal：這一輪「照設計」本來就該公布完整答案——不是用回覆長度去猜的，是
        # 用我們已經明確告訴模型的規則去判斷：直接要答案的暗號，或上一輪引導過這輪要收斂。
        # 這個旗標同時決定要不要送 note、以及等一下要不要對這輪的回覆強制補「資料來源」。
        already_revealed = None
        if history:
            last_reply = history[-1].get("a", "")
            already_revealed = ("資料來源" in last_reply) or ("Sources" in last_reply)
        should_reveal = is_direct_answer_request or (history is not None and history and not already_revealed)

        note = ""
        if is_direct_answer_request:
            note = (
                "\n\n[System note: the student's message is a terse direct request for the answer "
                "(e.g. just \"answer\"/\"ans\"). Reveal the complete answer now per rule 4(b) — do not "
                "guide again, and do not ask the student to clarify what they mean.]"
                if is_eng else
                "\n\n【系統提示：學生這則訊息是直接要求公布答案的簡短暗號（例如只打「答」）。"
                "請依規則4(b)這次直接公布完整答案，不要再引導，也不用反問學生是什麼意思。】"
            )
        elif should_reveal:
            note = (
                "\n\n[System note: your previous reply was a guiding hint, not the full answer. "
                "If this message continues the same question, you must reveal the complete answer now per rule 4 — do not guide again.]"
                if is_eng else
                "\n\n【系統提示：你上一輪回覆是引導反問，還沒有公布答案。如果這一則訊息是延續同一題，"
                "這次依規則4必須公布完整答案，不要再繼續引導。】"
            )
        if note:
            user_question += note

        messages.append({"role": "user", "content": user_question})

        resp = client.chat.completions.create(
            model=_SMART_QA_MODEL,
            messages=messages,
            max_completion_tokens=800,
        )
        base_reply = (resp.choices[0].message.content or "").strip()[:800]
        # 社交短句判斷：GPT 回應很短且不含資料來源標記，視為社交回應，不補 disclaimer
        _is_social_reply = len(base_reply) < 120 and "資料來源" not in base_reply and "Sources" not in base_reply
        if _is_social_reply:
            ai_reply = base_reply
        elif should_reveal:
            # 這輪照規則本來就該公布答案：確保真的有「資料來源」這行，模型自己漏掉的話用這個補上
            base_reply = _ensure_sources_section(base_reply, english=is_eng)
            ai_reply = base_reply + disclaimer
        else:
            # 這輪照規則本來就該是「引導」，不該有來源——就算模型講得比較長（gpt-5.4 常見），
            # 也不要因為長度誤判成「這是完整答案」硬補一行「資料來源：無」上去。
            ai_reply = base_reply + disclaimer

        # MongoDB 寫入改為背景非同步（不阻塞答復流程）
        threading.Thread(
            target=_log_interaction_to_mongodb_async,
            args=(user_id, text, ai_reply, is_eng),
            daemon=True,
        ).start()

        # 回覆：只回覆答案，根據 FORCE_PUSH_MODE 決定是否 push。
        ai_msg = text_with_quick_reply(ai_reply)
        try:
            if FORCE_PUSH_MODE:
                line_bot_api.push_message(user_id, ai_msg)
            elif reply_token:
                line_bot_api.reply_message(reply_token, ai_msg)
            else:
                line_bot_api.push_message(user_id, ai_msg)
        except Exception as e:
            print(f">>> DEBUG: tcm reply/push failed err={e}")

        try:
            # 設定使用者語言偏好（reply 之後，不在 critical path）
            _set_user_language(user_id, "en" if is_eng else "zh")
            log_question(redis, user_id, text)
            set_last_question(redis, user_id, text)
            set_last_assistant_message(redis, user_id, ai_reply)
            append_conv_history(redis, user_id, txt, base_reply)
        except Exception:
            pass

        return True
    except Exception:
        traceback.print_exc()
        return False

# --- 每週報告 Cron（需 CRON_SECRET 驗證）---
try:
    from api.weekly_report import run_weekly_report
except ImportError:
    from weekly_report import run_weekly_report

@app.route("/api/cron/weekly", methods=['GET', 'POST'])
def cron_weekly_report():
    """每週固定時間由 Vercel Cron 或外部排程呼叫，產出 PDF 並寄送至 REPORT_EMAIL。"""
    secret = request.headers.get("Authorization") or request.args.get("secret") or ""
    expected = os.getenv("CRON_SECRET", "")
    if expected and secret != expected and secret != "Bearer " + expected:
        return "Unauthorized", 401
    try:
        ok, msg = run_weekly_report(redis, client, mongo_db=mongo_db)
        return (msg, 200) if ok else (msg, 500)
    except Exception as e:
        traceback.print_exc()
        return str(e)[:200], 500

# --- 路由設定 ---
@app.route("/", methods=['GET'])
def home():
    return 'Line Bot Server is running!', 200

@app.route("/favicon.ico", methods=['GET'])
@app.route("/favicon.png", methods=['GET'])
def favicon():
    """避免瀏覽器/爬蟲請求 favicon 產生 404 日誌。"""
    return "", 204


_ASSETS_PATIENTS_DIR = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "assets", "patients")
_ASSETS_PERSONAS_DIR = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "assets", "personas")

@app.route("/assets/patients/<path:filename>", methods=['GET'])
def patient_photo(filename):
    """口說 LIFF 用的病人靜態照片，依 clinical_cases.json 的 case_id 對應檔名。"""
    from flask import send_from_directory
    return send_from_directory(_ASSETS_PATIENTS_DIR, filename)

@app.route("/assets/personas/<path:filename>", methods=['GET'])
def persona_photo(filename):
    """口說 LIFF 用的固定角色照片（教授／學生夥伴，不隨案例變動）。"""
    from flask import send_from_directory
    return send_from_directory(_ASSETS_PERSONAS_DIR, filename)


# ============================================================
# LIFF：國考題庫練習系統
# ============================================================
# 業務邏輯（題庫、評分、錯題本）都在 api/exam_quiz.py，這裡只負責：
# 1. 提供 LIFF 前端頁面（純靜態 HTML，前端用 liff SDK 處理登入與 API 呼叫）
# 2. 用 LIFF ID Token 驗證使用者身份（api/liff_auth.py），不信任前端自己宣稱的 userId
# 3. 把 exam_quiz.py 的回傳結果包成 JSON——注意這些函式本身不呼叫 line_bot_api，
#    跟現有 LINE webhook 業務邏輯（_tcm_openai_reply 等）耦合 push_message 的寫法不同，
#    這是刻意的，對應 README「平台策略」那節的架構解耦方向。

def _liff_auth_user_id():
    """
    從 Authorization: Bearer <idToken> 驗證 LIFF 使用者身份，回傳 user_id；驗證失敗回傳 None。

    本機開發用後門：若設定 LIFF_DEBUG_TOKEN 環境變數，且傳入的 token 剛好等於這個值，
    直接放行為固定的 "debug-user"，跳過真正打 LINE 驗證端點——只給 `?debug=1` 的本機
    ngrok 預覽用。LIFF_DEBUG_TOKEN 沒設定時這段完全不影響原本邏輯，正式環境（Railway）
    絕對不要設這個環境變數。
    """
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    if not token:
        return None
    debug_token = os.getenv("LIFF_DEBUG_TOKEN", "").strip()
    if debug_token and token == debug_token:
        return "debug-user"
    result = verify_liff_id_token(token)
    return result["user_id"] if result else None


def _exam_explain_answer(question_text, correct_answer_text, user_followup=None):
    """
    國考題「詳解 / AI 即刻問」：跟聊天室問答共用同一套「國考題庫優先、教材補充」檢索邏輯
    （_build_exam_grounded_context）當 context，純函式、回傳文字，不寫入 LINE。
    失敗回傳空字串，由呼叫端決定如何提示使用者。
    """
    if not (question_text or "").strip():
        return ""
    base_ctx = _build_exam_grounded_context(question_text)
    if user_followup and user_followup.strip():
        user_prompt = (
            f"[背景資料]\n{base_ctx}\n\n[題目]\n{question_text}\n\n[正確答案]\n{correct_answer_text}\n\n"
            f"[學生追問]\n{user_followup.strip()}\n\n請根據背景資料簡潔回答學生的追問。"
        )
    else:
        user_prompt = (
            f"[背景資料]\n{base_ctx}\n\n[題目]\n{question_text}\n\n[正確答案]\n{correct_answer_text}\n\n"
            f"請說明這一題的詳解：為什麼答案是這個選項，並簡要指出其他選項錯在哪裡。"
        )
    try:
        resp = client.chat.completions.create(
            model=_SMART_QA_MODEL,
            messages=[
                {"role": "system", "content": _TCM_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=600,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        traceback.print_exc()
        return ""


def _exam_explain_structured(question_text, options, correct_answer_text):
    """
    國考題「查看完整詳解」：回傳結構化 JSON（考點核心／君臣佐使或對應的細項比較／
    高頻陷阱／口訣／經典引證／延伸考點／追問建議），給 LIFF 卡片式排版用。
    breakdown 的角色標籤（君臣佐使等）只在題目是方劑組成時才有意義，其餘題型
    （穴位、辨證等）由模型自訂最貼切的 breakdown_title，role 留空字串即可。
    跟聊天室問答共用同一套「國考題庫優先、教材補充」檢索邏輯（_build_exam_grounded_context）。
    失敗回傳 None，由呼叫端決定如何提示使用者。
    """
    if not (question_text or "").strip():
        return None
    base_ctx = _build_exam_grounded_context(question_text)
    options_text = "\n".join(f"{k}. {v}" for k, v in (options or {}).items())
    user_prompt = (
        f"[背景資料]\n{base_ctx}\n\n[題目]\n{question_text}\n\n[選項]\n{options_text}\n\n"
        f"[正確答案]\n{correct_answer_text}\n\n"
        "請以下列 JSON 結構詳解這一題，給準備中醫師國考的學生看。只能輸出 JSON，不要任何多餘文字：\n"
        "{\n"
        '  "core": "考點與答案核心：一段話說明為什麼答案是這個選項",\n'
        '  "breakdown_title": "這組細項的標題，視題目性質自訂最貼切的名稱，'
        '例如方劑題用「君臣佐使．配伍深析」、穴位題用「相關穴位解析」、辨證題用「鑑別診斷要點」",\n'
        '  "breakdown": [{"role": "角色標籤，例如君藥／臣藥，不適用留空字串", '
        '"name": "藥物／穴位／證型等名稱", "tag": "簡短屬性，例如性味歸經", "note": "一兩句說明"}],\n'
        '  "trap_title": "若這題有常見的混淆點，給一個標題，例如「國考高頻陷阱：A vs B」；沒有就填 null",\n'
        '  "trap_note": "混淆點的具體說明；沒有就填 null",\n'
        '  "mnemonic": "好記的口訣；沒有就填 null",\n'
        '  "citation_source": "引用出處，例如《傷寒論》第12條；沒有就填 null",\n'
        '  "citation_quote": "原文引用；沒有就填 null",\n'
        '  "extension": "延伸考點或常考變化題提示；沒有就填 null",\n'
        '  "follow_ups": ["最多三個學生可能會想追問的延伸問題，每個不超過25字"]\n'
        "}\n"
        "breakdown 陣列依題目性質決定要不要放內容，完全不適用就給空陣列 []。"
    )
    try:
        resp = client.chat.completions.create(
            model=_SMART_QA_MODEL,
            messages=[
                {"role": "system", "content": _TCM_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=900,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        traceback.print_exc()
        return None


@app.route("/liff/quiz", methods=['GET'])
def liff_quiz_page():
    """LIFF 考題頁入口：回傳靜態 HTML，登入與 API 呼叫都在前端 JS 處理。"""
    try:
        path = os.path.join(os.path.dirname(__file__), "templates", "liff_quiz.html")
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        return Response(html, mimetype="text/html")
    except Exception:
        traceback.print_exc()
        return "LIFF page not found", 404


@app.route("/api/liff/quiz/categories", methods=['GET'])
def liff_quiz_categories():
    """回傳題庫目前有的章節分類（線框圖上方「選類別」下拉選單第二層：依章節）。"""
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"categories": exam_list_categories()})


@app.route("/api/liff/quiz/periods", methods=['GET'])
def liff_quiz_periods():
    """回傳「依年度」可選的梯次清單（線框圖上方「選類別」下拉選單第二層：依年度）。"""
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"periods": exam_list_exam_periods()})


@app.route("/api/liff/quiz/questions", methods=['GET'])
def liff_quiz_questions():
    """
    回傳題目（不含答案）。scope=chapter 用 category 篩選；scope=year 用
    exam_year/exam_session/exam_stage 篩選（見 /api/liff/quiz/periods 的清單）。
    """
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    scope = (request.args.get("scope") or "chapter").strip()
    if scope == "year":
        exam_year = (request.args.get("exam_year") or "").strip()
        exam_session = (request.args.get("exam_session") or "").strip()
        exam_stage = (request.args.get("exam_stage") or "").strip()
        if not exam_year or not exam_stage:
            return jsonify({"error": "缺少 exam_year 或 exam_stage"}), 400
        return jsonify({"questions": exam_get_questions_by_period(exam_year, exam_session, exam_stage)})
    category = (request.args.get("category") or "").strip()
    return jsonify({"questions": exam_get_questions_by_category(category or None)})


@app.route("/api/liff/quiz/questions/<question_id>", methods=['GET'])
def liff_quiz_question_answer(question_id):
    """
    單題詳細內容（含正確答案），給作答中「顯示答案」開關按需查詢用。
    刻意不在 /api/liff/quiz/questions 的列表裡直接附答案，避免答案一次全部送到前端。
    """
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    q = exam_get_question_detail(question_id)
    if not q:
        return jsonify({"error": "題目不存在"}), 404
    return jsonify(q)


@app.route("/api/liff/quiz/submit", methods=['POST'])
def liff_quiz_submit():
    """
    交卷評分：body = {"category": str, "question_ids": [str,...], "answers": {question_id: "A"/"B"/"C"/"D"}}。
    question_ids 是這次出給使用者的完整題目清單，分數以這份清單的題數當分母
    （沒作答的題目算答錯），不是只算使用者實際填了答案的題目，避免只答一題
    答對就變成 100 分。
    """
    user_id = _liff_auth_user_id()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    category = (data.get("category") or "").strip()
    question_ids = data.get("question_ids") or []
    answers = data.get("answers") or {}
    if not isinstance(question_ids, list) or not question_ids:
        return jsonify({"error": "question_ids 不可為空"}), 400
    if not isinstance(answers, dict):
        return jsonify({"error": "answers 格式錯誤"}), 400
    result = exam_submit_exam(mongo_db, user_id, category, question_ids, answers)
    return jsonify(result)


@app.route("/api/liff/quiz/bookmark", methods=['POST'])
def liff_quiz_bookmark():
    """作答中手動標記／取消標記單題到錯題本：body = {"question_id": str}。"""
    user_id = _liff_auth_user_id()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    question_id = (data.get("question_id") or "").strip()
    if not question_id:
        return jsonify({"error": "缺少 question_id"}), 400
    bookmarked = exam_toggle_bookmark(mongo_db, user_id, question_id)
    if bookmarked is None:
        return jsonify({"error": "資料庫未連線"}), 500
    return jsonify({"bookmarked": bookmarked})


@app.route("/api/liff/quiz/wrong-questions", methods=['GET'])
def liff_quiz_wrong_questions():
    """錯題本清單，可用 ?category=xxx 篩單一章節。"""
    user_id = _liff_auth_user_id()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    category = (request.args.get("category") or "").strip() or None
    return jsonify({"questions": exam_list_wrong_questions(mongo_db, user_id, category)})


@app.route("/api/liff/quiz/wrong-questions/<question_id>", methods=['DELETE'])
def liff_quiz_wrong_question_delete(question_id):
    """從錯題本移除單題（對應線框圖再按一次標記圖示）。"""
    user_id = _liff_auth_user_id()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    removed = exam_remove_wrong_question(mongo_db, user_id, question_id)
    return jsonify({"removed": removed})


@app.route("/api/liff/quiz/explain", methods=['POST'])
def liff_quiz_explain():
    """詳解 / AI 即刻問：body = {"question_id": str, "follow_up": str(optional)}。"""
    user_id = _liff_auth_user_id()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    question_id = (data.get("question_id") or "").strip()
    follow_up = (data.get("follow_up") or "").strip()
    q = exam_get_question_detail(question_id)
    if not q:
        return jsonify({"error": "題目不存在"}), 404
    answer_letter = q.get("answer") or ""
    answer_text = f"{answer_letter} {(q.get('options') or {}).get(answer_letter, '')}".strip()
    if follow_up:
        reply = _exam_explain_answer(q.get("question", ""), answer_text, follow_up)
        if not reply:
            return jsonify({"error": "AI 回覆失敗，請再試一次"}), 500
        return jsonify({"reply": reply})
    structured = _exam_explain_structured(q.get("question", ""), q.get("options"), answer_text)
    if not structured:
        return jsonify({"error": "AI 回覆失敗，請再試一次"}), 500
    structured["question"] = q.get("question", "")
    structured["answer"] = answer_letter
    structured["answer_text"] = (q.get("options") or {}).get(answer_letter, "")
    structured["category"] = q.get("category", "")
    return jsonify(structured)


# ============================================================
# LIFF：NPC 對話式口說教練
# ============================================================
# 語音對話走 OpenAI Realtime API，前端瀏覽器直接用 WebRTC 連線到 OpenAI（延遲最低，
# 我們的伺服器不中繼音訊）。這裡的路由只做三件事：
# 1. 提供 LIFF 前端頁面
# 2. 用主 API Key 換一組短效 ephemeral client secret 給前端（絕不能把主 Key 交給瀏覽器）
# 3. 逐輪／結算頁的文字錯誤標註（純文字 chat.completions，跟語音對話是分開的兩條路徑）
# 對話逐字稿刻意不寫入 MongoDB／Redis——只在瀏覽器這次 session 存在，離開結算頁就清空。

@app.route("/liff/speaking", methods=['GET'])
def liff_speaking_page():
    """LIFF 口說教練頁入口：回傳靜態 HTML。"""
    try:
        path = os.path.join(os.path.dirname(__file__), "templates", "liff_speaking.html")
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        return Response(html, mimetype="text/html")
    except Exception:
        traceback.print_exc()
        return "LIFF page not found", 404


@app.route("/api/liff/speaking/scenarios", methods=['GET'])
def liff_speaking_scenarios():
    """主畫面兩個主題按鈕的資料（臨床衛教／患者、學術討論／教授）。"""
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"scenarios": speaking_list_modes()})


@app.route("/api/liff/speaking/cases", methods=['GET'])
def liff_speaking_cases():
    """『選擇病患情境』清單：列出全部臨床病例，讓使用者指定要練習哪一個，而不是每次隨機抽。"""
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"cases": speaking_list_cases()})


@app.route("/api/liff/speaking/session", methods=['POST'])
def liff_speaking_session():
    """
    開始一段對話：body = {"mode": "clinical"|"academic"|"student", "difficulty": str(optional),
    "case_id": str(optional，只對 clinical 有意義，使用者從清單指定病例時帶這個)}。
    後端會依 case_id 指定或動態抽一個 Case（臨床衛教）或 Topic（學術討論），組出 Realtime
    session 的角色 instructions，並建立一個 Session Manager 記錄（session_id）。
    回傳 ephemeral client_secret，前端用它直接對 OpenAI 建立 WebRTC 連線，不經過我們的伺服器。
    """
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    mode = (data.get("mode") or data.get("scenario") or "").strip()
    difficulty = (data.get("difficulty") or "").strip() or None
    case_id = (data.get("case_id") or "").strip() or None
    session_info = speaking_mint_ephemeral_session(client, redis, mode, difficulty, case_id)
    if not session_info:
        return jsonify({"error": "無法建立語音對話 session，請再試一次"}), 500
    return jsonify(session_info)


@app.route("/api/liff/speaking/turn", methods=['POST'])
def liff_speaking_turn():
    """
    逐輪 structured response：body = {"session_id": str, "assistant_text": str, "user_text": str}。
    assistant_text／user_text 是這一輪 Realtime API 語音對話產生的逐字稿（語音本身已經播放過，
    這裡只做事後分析）。回傳規格書格式的物件：translation／language_feedback／terminology／
    hint／session_state／avatar（avatar 欄位 Phase 1 先固定為 null，留給 Phase 2 用）。
    """
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    assistant_text = data.get("assistant_text") or ""
    user_text = data.get("user_text") or ""
    if not session_id:
        return jsonify({"error": "缺少 session_id"}), 400
    result = speaking_process_turn(client, redis, session_id, assistant_text, user_text)
    return jsonify(result)


@app.route("/api/liff/speaking/summary", methods=['POST'])
def liff_speaking_summary():
    """
    結算頁摘要：body = {"session_id": str(optional), "transcript": [{"role": "user"|"assistant", "text": str}, ...]}。
    回傳 {clinical_precision, lexicon_accuracy, cases}（見 speaking_evaluator.summarize_session）。
    對應線框圖「正常交卷才存檔」——這裡同時清掉該 session 在 Redis 裡的 Session Manager 記錄。
    """
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    transcript = data.get("transcript") or []
    session_id = (data.get("session_id") or "").strip()
    if not isinstance(transcript, list):
        return jsonify({"error": "transcript 格式錯誤"}), 400
    summary = speaking_build_session_summary(client, transcript)
    if session_id:
        speaking_end_session(redis, session_id)
    return jsonify(summary)


# ============================================================
# LIFF：Writing Coach（即時標註＋送出批改）
# ============================================================
# 即時標註（Grammarly 風格）與送出批改都是純文字 chat.completions，不影響 LINE 現有的
# _revision_handler。儲存的練習紀錄寫進 MongoDB writing_practice——跟口說 LIFF 不同，
# 這裡的資料是明確要求要保留的（見線框圖「儲存練習內容」「一鍵同意/確認儲存」）。

@app.route("/liff/writing", methods=['GET'])
def liff_writing_page():
    """LIFF 寫作教練頁入口：回傳靜態 HTML。"""
    try:
        path = os.path.join(os.path.dirname(__file__), "templates", "liff_writing.html")
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        return Response(html, mimetype="text/html")
    except Exception:
        traceback.print_exc()
        return "LIFF page not found", 404


@app.route("/api/liff/writing/topics", methods=['GET'])
def liff_writing_topics():
    """主畫面題目/範本清單（含「自由寫作」）。"""
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"topics": writing_load_topics()})


@app.route("/api/liff/writing/check", methods=['POST'])
def liff_writing_check():
    """
    即時逐字標註：body = {"text": str} → {"annotated": ...}。
    由前端 debounce 後呼叫（見 liff_writing.html，停止打字約 0.9 秒才送出），避免每個按鍵都打 API。
    """
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text") or ""
    annotated = writing_annotate_realtime(client, text)
    return jsonify({"annotated": annotated})


@app.route("/api/liff/writing/review", methods=['POST'])
def liff_writing_review():
    """送出批改：body = {"text": str, "topic_id": str(optional)} → 完整評分＋修正版＋說明。"""
    if not _liff_auth_user_id():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    topic_id = (data.get("topic_id") or "").strip() or None
    if not text:
        return jsonify({"error": "請先輸入內容再送出批改"}), 400
    result = writing_full_review(client, text, topic_id)
    if not result:
        return jsonify({"error": "AI 批改失敗，請再試一次"}), 500
    return jsonify(result)


@app.route("/api/liff/writing/save", methods=['POST'])
def liff_writing_save():
    """
    儲存練習內容：body = {"text": str, "topic_id": str(optional), "kind": "draft"|"reviewed",
    "review": dict(optional)}。
    對應線框圖兩顆按鈕——「儲存練習內容」傳 kind=draft（存使用者自己打的原文，進草稿箱），
    「一鍵同意/確認儲存」傳 kind=reviewed（存 AI 修正後版本，可一併附上 /review 回傳的
    完整評分結果，進過往練習記錄，不用重新呼叫 AI 就能顯示分數）。
    """
    user_id = _liff_auth_user_id()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    topic_id = (data.get("topic_id") or "").strip() or None
    kind = (data.get("kind") or "draft").strip()
    review = data.get("review") if isinstance(data.get("review"), dict) else None
    if not text:
        return jsonify({"error": "內容不可為空"}), 400
    saved_id = writing_save_practice(mongo_db, user_id, topic_id, text, kind, review)
    if not saved_id:
        return jsonify({"error": "儲存失敗（資料庫未連線或參數錯誤）"}), 500
    return jsonify({"saved_id": saved_id})


@app.route("/api/liff/writing/practice", methods=['GET'])
def liff_writing_practice_list():
    """草稿箱／過往練習記錄共用清單：?kind=draft 或 ?kind=reviewed，不帶就回全部。"""
    user_id = _liff_auth_user_id()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    kind = (request.args.get("kind") or "").strip() or None
    return jsonify({"items": writing_list_practice(mongo_db, user_id, kind)})


@app.route("/api/liff/writing/practice/<practice_id>", methods=['DELETE'])
def liff_writing_practice_delete(practice_id):
    """捨棄一筆練習紀錄（草稿箱「捨棄草稿」按鈕）。"""
    user_id = _liff_auth_user_id()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    removed = writing_discard_practice(mongo_db, user_id, practice_id)
    return jsonify({"removed": removed})


def _run_voice_background(user_id, message_id, base_url, cron_secret):
    """Background Task：語音轉錄、GPT 分析、TTS、Cloudinary 上傳。不阻塞 webhook 回傳。"""
    if base_url and cron_secret:
        try:
            requests.post(
                f"{base_url}/api/process-voice-async",
                json={"user_id": user_id, "message_id": message_id},
                headers={"Authorization": f"Bearer {cron_secret}"},
                timeout=30,
            )
        except Exception:
            try:
                _process_voice_sync(user_id, message_id)
            except Exception:
                traceback.print_exc()
    else:
        try:
            _process_voice_sync(user_id, message_id)
        except Exception:
            traceback.print_exc()


def _process_voice_sync(user_id, message_id, mode=None):
    """
    語音處理：Whisper 辨識轉文字，就當成使用者打字問了這個問題，直接走跟文字訊息
    一樣的中醫問答模組（_tcm_openai_reply）。用 push_message 回傳，錯誤時主動 push
    友善提示。
    舊版「Speaking 模式」的 Azure 發音評估／練習句／TTS 示範語音整套都已經移除
    ——口說練習現在是 LIFF 頁面（Realtime API + WebRTC），跟這裡完全不同的架構。
    """
    if not user_id or not str(user_id).strip():
        print(f"[VOICE] ERROR: user_id invalid user_id={repr(user_id)}")
        return
    try:
        print(f"[VOICE] start user_id={user_id} message_id={message_id}")
        message_content = line_bot_api.get_message_content(message_id)

        audio_chunks = list(message_content.iter_content())
        audio_bytes = b"".join(audio_chunks)
        tmp_dir = tempfile.gettempdir()
        temp_path = os.path.join(tmp_dir, f"{message_id}.m4a")
        try:
            with open(temp_path, "wb") as f:
                f.write(audio_bytes)
        except Exception:
            temp_path = os.path.join(os.path.dirname(__file__) or ".", f"{message_id}.m4a")
            with open(temp_path, "wb") as f:
                f.write(audio_bytes)

        with open(temp_path, "rb") as audio_file:
            _whisper_lang = "en" if FORCE_LANG == "en" else None
            _whisper_kwargs = {"model": "whisper-1", "file": audio_file}
            if _whisper_lang:
                _whisper_kwargs["language"] = _whisper_lang
            transcript = client.audio.transcriptions.create(**_whisper_kwargs)
        if os.path.isfile(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

        transcript_text = (transcript.text or "").strip()
        transcription_msg = f"🎤 Recognized: \"{transcript_text}\"" if FORCE_LANG == "en" else f"🎤 辨識內容：「{transcript_text}」"
        line_bot_api.push_message(user_id, TextSendMessage(text=transcription_msg))

        if is_off_topic(transcript_text):
            line_bot_api.push_message(user_id, text_with_quick_reply(OFF_TOPIC_REPLY))
        else:
            _tcm_openai_reply(user_id, transcript_text)
        print(f"[VOICE] done")
    except Exception as e:
        print(f"[VOICE] CRITICAL err={e}")
        traceback.print_exc()
        try:
            line_bot_api.push_message(user_id, text_with_quick_reply("❌ 語音辨識失敗，請再試一次。"))
        except Exception:
            pass


@app.route("/api/process-voice-async", methods=["POST"])
def process_voice_async():
    """Background Task：接收語音 message_id，執行 Whisper 轉文字 -> 中醫問答模組 -> push。"""
    secret = request.headers.get("Authorization") or request.headers.get("X-Internal-Secret") or ""
    expected = os.getenv("CRON_SECRET", "")
    if expected and secret not in (expected, "Bearer " + expected):
        return "Unauthorized", 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = (data.get("user_id") or "").strip()
        message_id = (data.get("message_id") or "").strip()
        if not user_id or not message_id:
            return "Missing user_id or message_id", 400
        _process_voice_sync(user_id, message_id)
        return "OK", 200
    except Exception as e:
        traceback.print_exc()
        try:
            line_bot_api.push_message(
                (request.get_json(force=True, silent=True) or {}).get("user_id", ""),
                text_with_quick_reply("❌ 語音辨識或處理失敗，請再試一次。"),
            )
        except Exception:
            pass
        return str(e)[:200], 500


@app.route("/callback", methods=['POST'])
def callback():
    """LINE Webhook 唯一入口（Railway 等長連線環境：直接執行 handle，gunicorn timeout 120s）。"""
    signature = request.headers.get('X-Line-Signature') or ''
    body = request.get_data(as_text=True) or ''
    try:
        line_webhook_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        traceback.print_exc()
    return Response('OK', status=200)

# --- 事件處理 ---
@line_webhook_handler.add(PostbackEvent)
def handle_postback(event):
    """
    Postback 事件現在沒有對應功能了——舊版測驗選項、課務查詢回饋星等、模式切換
    按鈕都已經移除（國考題庫／口說教練／寫作教練都改成 LIFF，Rich Menu 是
    uri 直連，不會觸發 Postback）。保留這個 handler 只是為了防呆：如果使用者
    裝置上還殘留舊版選單、誤觸發了 Postback，不要噴 500，給個友善提示。
    """
    user_id = event.source.user_id
    try:
        line_bot_api.reply_message(
            event.reply_token,
            text_with_quick_reply("這個功能已經更新囉，請用下方選單開啟考題／口說／寫作頁面。"),
        )
    except Exception:
        traceback.print_exc()

@line_webhook_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """
    聊天室現在只做一件事：所有文字訊息都直接走中醫問答模組（蘇格拉底式引導，
    見 _TCM_SYSTEM_PROMPT）。舊版的小測驗、口說練習、寫作修訂、課務查詢等
    模式切換與功能已經移除——國考題庫／口說教練／寫作教練都改成 LIFF 頁面，
    見 Rich Menu 三個入口。
    """
    user_id = event.source.user_id
    user_text = (event.message.text or "").strip()
    try:
        _start_loading_indicator(user_id)
        if not _tcm_openai_reply(user_id, user_text, reply_token=event.reply_token):
            try:
                line_bot_api.reply_message(event.reply_token, text_with_quick_reply("An error occurred, please try again." if FORCE_LANG == "en" else "處理時發生錯誤，請稍後再試。"))
            except Exception:
                pass
    except Exception as e:
        traceback.print_exc()
        err_msg = str(e).strip()[:100]
        try:
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(f"處理訊息時發生錯誤，請再試一次。（{err_msg}）"))
        except Exception:
            try:
                line_bot_api.push_message(user_id, text_with_quick_reply(f"處理訊息時發生錯誤，請再試一次。（{err_msg}）"))
            except Exception:
                pass

@line_webhook_handler.add(MessageEvent, message=AudioMessage)
def handle_audio(event):
    """語音訊息：立即回覆釋放 token，背景/同步轉文字後走中醫問答模組。"""
    user_id = event.source.user_id
    message_id = event.message.id

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="Converting voice, please wait... 🎙️" if FORCE_LANG == "en" else "正在轉換語音，請稍候... 🎙️"),
    )

    print(f"[VOICE] running sync (worker) user_id={user_id}")
    _process_voice_sync(user_id, message_id)


@line_webhook_handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    """圖片分析：下載圖片 → GPT-4o-mini vision 擷取內容 → 走中醫問答邏輯。"""
    user_id = event.source.user_id
    message_id = event.message.id

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="Analyzing image, please wait... 🖼️" if FORCE_LANG == "en" else "圖片分析中，請稍候... 🖼️"),
    )

    try:
        # 下載圖片並轉 base64
        content = line_bot_api.get_message_content(message_id)
        image_data = b"".join(chunk for chunk in content.iter_content())
        image_b64 = base64.b64encode(image_data).decode("utf-8")

        # GPT-4o-mini vision：擷取圖片內容描述
        if FORCE_LANG == "en":
            vision_prompt = (
                "Describe the content of this image concisely. "
                "If it contains text, transcribe it. "
                "If it shows medical, anatomical, or TCM-related content (e.g., tongue, acupoints, herbs, charts), describe those specifically. "
                "Keep the description factual and under 300 words."
            )
        else:
            vision_prompt = (
                "請簡潔描述這張圖片的內容。"
                "如果有文字，請轉錄出來。"
                "如果包含醫療、解剖或中醫相關內容（如舌診、穴位圖、藥材、表格等），請特別說明。"
                "保持客觀描述，300字以內。"
            )

        vision_resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    ],
                }
            ],
            max_tokens=400,
        )
        image_description = (vision_resp.choices[0].message.content or "").strip()
        print(f"[IMAGE] vision description len={len(image_description)} user_id={user_id}")

        if not image_description:
            err_msg = "Sorry, I couldn't analyze the image. Please try again." if FORCE_LANG == "en" else "抱歉，無法分析圖片，請再試一次。"
            line_bot_api.push_message(user_id, text_with_quick_reply(err_msg))
            return

        # 通知使用者圖片被識別的摘要，再走中醫問答
        if FORCE_LANG == "en":
            notice = f"📷 Image recognized:\n{image_description}\n\nAnalyzing from a TCM perspective..."
        else:
            notice = f"📷 圖片識別結果：\n{image_description}\n\n正在從中醫角度分析..."
        line_bot_api.push_message(user_id, TextSendMessage(text=notice))

        # 以圖片描述作為問題走中醫問答邏輯
        if FORCE_LANG == "en":
            tcm_query = f"The student sent an image. Content: {image_description}\n\nPlease analyze and explain this from a TCM perspective."
        else:
            tcm_query = f"學生傳送了一張圖片，圖片內容如下：\n{image_description}\n\n請從中醫的角度進行分析與說明。"

        _tcm_openai_reply(user_id, tcm_query, reply_token=None)

    except Exception as e:
        print(f"[IMAGE] CRITICAL err={e}")
        traceback.print_exc()
        try:
            err_msg = "Sorry, image processing failed. Please try again." if FORCE_LANG == "en" else "圖片處理失敗，請再試一次。"
            line_bot_api.push_message(user_id, text_with_quick_reply(err_msg))
        except Exception:
            pass


if __name__ == "__main__":
    # 本地快速測試：python -m api.index 或 python api/index.py（從專案根目錄）
    # 再開一個終端執行 ngrok http 5000，並將 LINE Webhook 改為 https://YOUR-NGROK-URL/callback
    app.run(host="0.0.0.0", port=5000, debug=True)
