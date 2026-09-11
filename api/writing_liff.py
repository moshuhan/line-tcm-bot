# -*- coding: utf-8 -*-
"""
Writing LIFF：即時逐字標註（Grammarly 風格）＋ 送出批改 ＋ 儲存練習內容。

現況與已知限制（都是刻意的簡化，先求可用，之後再擴充）：
- 「評分標準」目前是寫死在 `_REVIEW_SYSTEM_PROMPT` 裡的暫定 rubric（內容正確性／學術語域／
  文法清晰度／段落結構各 25 分），不是真正跟 TEEMI 做區隔用的正式評分標準——等老師/顏老師
  那邊有實際的評分規準文件，再回來換掉這個 prompt。
- 「主題／範本」目前只有 `data/writing_prompts.json` 裡兩篇中醫中文段落（中譯英摘要練習）
  ＋ 一個「自由寫作」選項，之後題庫擴充時比照 exam_quiz.py 的模式直接加進 JSON 檔案即可。
- 儲存的練習紀錄寫入 MongoDB `writing_practice`（跟口說 LIFF 不同——寫作是明確要求要儲存的）。
"""
import os
import json
import traceback
from datetime import datetime, timezone

COLL_WRITING_PRACTICE = "writing_practice"

_TOPICS_CACHE = None

# 暫定評分標準（見檔頭說明：不是正式版，等實際 rubric 文件）
_REVIEW_SYSTEM_PROMPT = """
你是一位嚴謹的中醫學術英文寫作教練。使用者會交來一段英文寫作（可能是中醫衛教文章摘要，
也可能是自由寫作），請依以下暫定評分標準批改：

【評分標準（各 25 分，總分 100，屬暫定版本，之後會替換成正式 rubric）】
1. 內容正確性（中醫概念/邏輯是否正確，若非中醫主題則看論述是否合理）
2. 學術語域與用詞精準度（是否用詞恰當、避免口語化）
3. 文法與表達清晰度
4. 段落結構與銜接

【輸出格式，嚴格回傳 JSON，不要其他文字】
{
  "scores": {"content": 0-25, "register": 0-25, "grammar": 0-25, "structure": 0-25},
  "total": 0-100,
  "praise": "1-2 句具體稱讚做得好的地方",
  "corrected": "完整修正後的版本",
  "explanation": "單一字串（不是陣列），內容用 \n 換行條列 2-4 點，說明主要修改了什麼、為什麼"
}
""".strip()


def _prompts_path():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "writing_prompts.json"))


def load_topics():
    """載入寫作題目/範本清單，記憶體快取（比照 exam_quiz.py 的模式）。"""
    global _TOPICS_CACHE
    if _TOPICS_CACHE is not None:
        return _TOPICS_CACHE
    path = _prompts_path()
    if not os.path.isfile(path):
        _TOPICS_CACHE = []
        return _TOPICS_CACHE
    try:
        with open(path, "r", encoding="utf-8") as f:
            _TOPICS_CACHE = json.load(f)
    except Exception:
        _TOPICS_CACHE = []
    return _TOPICS_CACHE


def get_topic(topic_id):
    for t in load_topics():
        if t.get("id") == topic_id:
            return t
    return None


def annotate_realtime(openai_client, text):
    """
    即時逐字標註（Grammarly 風格）：回傳「跟輸入文字完全一樣、只在錯誤片段前後加上
    « 與 »」的字串。前端會拿這個結果跟目前 textarea 內容比對（去掉 « » 後必須完全一致），
    一致才更新標色層，避免因為 LLM 沒有嚴格保留原文而讓標色跟游標位置對不齊。
    失敗或文字太短時回傳原文（視為沒有標註）。
    """
    text = (text or "")
    if len(text.strip()) < 8:
        return text
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "You proofread a TCM (Traditional Chinese Medicine) student's English writing for "
                    "(1) grammar/word-choice/register errors and (2) TCM content errors. "
                    "Return the text EXACTLY as given, character for character, ONLY wrapping erroneous "
                    "word(s) or phrase(s) with « and ». Do not paraphrase, do not fix anything silently, "
                    "do not add or remove any other character, do not add explanation. "
                    "If there is no error, return the text completely unchanged."
                )},
                {"role": "user", "content": text[:2000]},
            ],
            max_tokens=800,
            temperature=0,
        )
        return (resp.choices[0].message.content or text)
    except Exception:
        traceback.print_exc()
        return text


def full_review(openai_client, text, topic_id=None):
    """
    送出批改：完整評分＋修正版＋說明。回傳 dict，失敗回傳 None。
    """
    text = (text or "").strip()
    if not text:
        return None
    topic = get_topic(topic_id) if topic_id else None
    user_content = text[:3000]
    if topic and topic.get("source_zh"):
        user_content = f"[題目來源中文段落]\n{topic['source_zh']}\n\n[使用者英文寫作]\n{user_content}"
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=1000,
            temperature=0.2,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if "```" in raw:
            parts = raw.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("json"):
                    p = p[4:].strip()
                if p.startswith("{"):
                    raw = p
                    break
        if "{" in raw and "}" in raw:
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
        obj = json.loads(raw)
        scores = obj.get("scores") or {}

        def _as_text(val):
            """explanation 等欄位模型偶爾會回傳條列 list 而非字串，統一轉成換行字串。"""
            if isinstance(val, list):
                return "\n".join(f"- {str(v).strip()}" for v in val if str(v).strip())
            return str(val or "").strip()

        return {
            "scores": {
                "content": int(scores.get("content", 0)),
                "register": int(scores.get("register", 0)),
                "grammar": int(scores.get("grammar", 0)),
                "structure": int(scores.get("structure", 0)),
            },
            "total": int(obj.get("total", 0)),
            "praise": _as_text(obj.get("praise")),
            "corrected": _as_text(obj.get("corrected")),
            "explanation": _as_text(obj.get("explanation")),
        }
    except Exception:
        traceback.print_exc()
        return None


def save_practice(db, user_id, topic_id, text, kind="draft"):
    """
    儲存練習內容。kind: "draft"（儲存練習內容按鈕，存使用者自己打的原文）
    或 "reviewed"（一鍵採用並儲存按鈕，存 AI 修正後版本）。
    回傳新增紀錄的 id（字串），db 無連線或參數不完整回傳 None。
    """
    if db is None or not user_id or not (text or "").strip():
        return None
    try:
        result = db[COLL_WRITING_PRACTICE].insert_one({
            "user_id": user_id,
            "topic_id": topic_id,
            "kind": kind,
            "text": text[:5000],
            "created_at": datetime.now(timezone.utc),
        })
        return str(result.inserted_id)
    except Exception:
        traceback.print_exc()
        return None
