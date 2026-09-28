# -*- coding: utf-8 -*-
"""
Writing LIFF：即時逐字標註（Grammarly 風格）＋ 送出批改 ＋ 儲存練習內容。

現況與已知限制：
- 「評分標準」寫死在 `_REVIEW_SYSTEM_PROMPT` 裡，四個面向對應 IELTS Writing Task 2 的
  官方評分規準（Task Response／Lexical Resource／Grammatical Range and Accuracy／
  Coherence and Cohesion），並疊加中醫學術寫作特有的內容正確性要求；之後老師/顏老師
  那邊有正式評分規準文件時，再回來對照微調用字。
- 「主題／範本」目前只有 `data/writing_prompts.json` 裡兩篇中醫中文段落（中譯英摘要練習）
  ＋ 一個「自由寫作」選項，之後題庫擴充時比照 exam_quiz.py 的模式直接加進 JSON 檔案即可
  （先求可用、之後補廣度，是刻意的取捨，不是遺漏）。
- 「自由寫作」沒有固定的中醫來源段落，送出批改前會先比照聊天室的 `is_off_topic()` 判斷
  離題，離題就直接回傳引導訊息、不呼叫 AI 批改（見 `full_review()`），避免評分失真也省
  token；有 source_zh/instruction_zh 的題目本身鎖定中醫來源，不需要這層檢查。
- 儲存的練習紀錄寫入 MongoDB `writing_practice`（跟口說 LIFF 不同——寫作是明確要求要儲存的）。
"""
import os
import json
import traceback
from datetime import datetime, timezone

try:
    from api.syllabus import is_off_topic
except ImportError:
    from syllabus import is_off_topic

COLL_WRITING_PRACTICE = "writing_practice"

# 自由寫作沒有固定的中醫來源段落，使用者可能打任何主題——比照聊天室的離題判斷邏輯
# （api/syllabus.py 的 is_off_topic），離題就直接擋下、不呼叫 AI 批改，省下無意義的
# token 花費，也避免評分結果對非中醫內容失真。有 source_zh/instruction_zh 的題目
# （SCI 摘要、中翻英）本身就鎖定中醫來源段落，不需要這層檢查。
OFF_TOPIC_WRITING_REPLY = (
    "這裡是中醫英文寫作練習，內容需要與中醫相關（例如中醫理論、證型、治療、衛教等主題）。"
    "麻煩把這段文字改寫成與中醫有關的內容再送出批改，這樣才能給你有意義的回饋喔！"
)

_TOPICS_CACHE = None

# 評分標準：比照 IELTS Writing Task 2 的四大評分面向（Task Response、Lexical Resource、
# Grammatical Range and Accuracy、Coherence and Cohesion）等比例改寫成 100 分制，
# 並在 Task Response 這一項加入中醫學術寫作特有的「內容正確性」要求（IELTS 本身不評
# 專業內容對錯，這裡是額外疊加的中醫專業把關）。細部用字仍會依老師/顏老師之後提供的
# 正式評分規準文件微調，但整體框架已經是對齊國際公認寫作測驗標準、而非隨意訂的暫定版。
_REVIEW_SYSTEM_PROMPT = """
你是一位嚴謹的中醫學術英文寫作教練。使用者會交來一段英文寫作（可能是中醫衛教文章摘要，
也可能是自由寫作），請依以下評分標準批改——四個面向分別對應 IELTS Writing Task 2 的
四大官方評分規準（Task Response／Lexical Resource／Grammatical Range and Accuracy／
Coherence and Cohesion），並疊加中醫學術寫作特有的內容正確性要求：

【評分標準（各 25 分，總分 100）】
1. content（內容正確性與任務達成度，對應 IELTS Task Response）：是否切題、論點是否充分
   開展並回應題目要求；中醫主題另需檢查中醫概念/邏輯是否正確，非中醫主題則看論述是否合理。
2. register（學術語域與用詞精準度，對應 IELTS Lexical Resource）：字彙廣度與精準度、
   詞語搭配（collocation）是否自然、是否避免口語化或不當重複用詞。
3. grammar（文法正確性與句式多樣性，對應 IELTS Grammatical Range and Accuracy）：
   句型結構是否多樣（含複合句、從屬子句等），文法錯誤是否影響語意理解。
4. structure（段落結構與銜接，對應 IELTS Coherence and Cohesion）：全文邏輯推進是否
   清楚、段落安排是否合理、銜接詞與指代（referencing）使用是否恰當。

【完整度是評分的前提，不是額外加分項】
使用者訊息裡如果有附上「題目要求」（字數/句數範圍、任務說明），批改前務必先比對使用者
寫的內容有沒有達到那個份量與任務要求。這是嚴重扣分甚至接近零分的情況，不能因為「這一兩句
文法正確」就給中等分數：
- 只寫了一句話、半句話，或明顯只是題目要求的一小部分（例如要求 80-120 字的摘要卻只寫了
  10 幾個字），視為「未完成」：content 與 structure 兩項最多給 0-5 分（不是 10 幾分），
  因為內容不完整、結構根本還沒成形；grammar 可以照那一兩句話本身的正確性給分，但 total
  必須整體反映「這份作業還沒寫完」，不能因為 grammar 拿滿分就把 total 拉高到及格帶。
- 字數/句數大致達到要求但明顯離題、漏掉關鍵論點，content 也要對應扣分，不能只看文法。
- 沒有附題目要求（例如自由寫作）時，才用一般寫作品質判斷，不用套用上述完整度規則。
在 explanation 裡明確指出「字數/句數不足」或「未完成」這件事，不要略過不提。

【輸出格式，嚴格回傳 JSON，不要其他文字】
{
  "scores": {"content": 0-25, "register": 0-25, "grammar": 0-25, "structure": 0-25},
  "total": 0-100,
  "praise": "1-2 句具體稱讚做得好的地方；若內容明顯未完成，這裡可以誠實說「篇幅太短還看不出完整表現」，不用勉強找優點",
  "corrected": "完整修正後的版本；若原文明顯不完整，就在補完的部分後面用 [示範補寫] 標註，讓使用者看得出來哪些是他自己寫的、哪些是你補的",
  "explanation": "單一字串（不是陣列），內容用 \n 換行條列 2-4 點，說明主要修改了什麼、為什麼；內容不完整時第一點就要點出字數/句數落差"
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
    離題（僅限沒有固定中醫來源段落的自由寫作）回傳 {"off_topic": True, "message": ...}，
    不呼叫 AI，前端要另外處理這個 key，不能直接當成正常評分結果渲染。
    """
    text = (text or "").strip()
    if not text:
        return None
    topic = get_topic(topic_id) if topic_id else None
    has_fixed_tcm_source = bool(topic and (topic.get("source_zh") or topic.get("instruction_zh")))
    if not has_fixed_tcm_source and is_off_topic(text):
        return {"off_topic": True, "message": OFF_TOPIC_WRITING_REPLY}
    word_count = len(text.split())
    user_content = text[:3000]
    if topic and (topic.get("source_zh") or topic.get("instruction_zh")):
        parts = []
        if topic.get("source_zh"):
            parts.append(f"[題目來源中文段落]\n{topic['source_zh']}")
        if topic.get("instruction_zh"):
            parts.append(f"[題目要求]\n{topic['instruction_zh']}")
        parts.append(f"[使用者英文寫作（約 {word_count} 個英文單字）]\n{user_content}")
        user_content = "\n\n".join(parts)
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


def save_practice(db, user_id, topic_id, text, kind="draft", review=None):
    """
    儲存練習內容。kind: "draft"（儲存練習內容按鈕，存使用者自己打的原文，給「草稿箱」列表用）
    或 "reviewed"（一鍵採用並儲存按鈕，或送出批改後自動存檔，給「過往練習記錄」列表用）。
    review：kind="reviewed" 時可附上 full_review() 的完整結果（scores/total/praise/...），
    「過往練習記錄」可以不用重新呼叫 AI 就能顯示分數。
    回傳新增紀錄的 id（字串），db 無連線或參數不完整回傳 None。
    """
    if db is None or not user_id or not (text or "").strip():
        return None
    try:
        doc = {
            "user_id": user_id,
            "topic_id": topic_id,
            "kind": kind,
            "text": text[:5000],
            "created_at": datetime.now(timezone.utc),
        }
        if review:
            doc["review"] = review
        result = db[COLL_WRITING_PRACTICE].insert_one(doc)
        return str(result.inserted_id)
    except Exception:
        traceback.print_exc()
        return None


def list_practice(db, user_id, kind=None):
    """
    列出使用者的練習紀錄（草稿箱／過往練習記錄共用，靠 kind 篩選），最新在前。
    回傳每筆 {id, topic_id, topic_title, kind, text, word_count, review, created_at(ISO字串)}。
    db 無連線或沒有 user_id 回傳空陣列。
    """
    if db is None or not user_id:
        return []
    query = {"user_id": user_id}
    if kind:
        query["kind"] = kind
    try:
        docs = list(db[COLL_WRITING_PRACTICE].find(query).sort("created_at", -1).limit(50))
    except Exception:
        return []
    out = []
    for d in docs:
        topic = get_topic(d.get("topic_id")) if d.get("topic_id") else None
        text = d.get("text") or ""
        created = d.get("created_at")
        out.append({
            "id": str(d.get("_id")),
            "topic_id": d.get("topic_id"),
            "topic_title": (topic or {}).get("title") or "自由寫作",
            "kind": d.get("kind", "draft"),
            "text": text,
            "word_count": len(text.split()),
            "review": d.get("review"),
            "created_at": created.isoformat() if hasattr(created, "isoformat") else None,
        })
    return out


def discard_practice(db, user_id, practice_id):
    """刪除一筆練習紀錄（草稿箱「捨棄草稿」）。回傳是否有刪除到資料。"""
    if db is None or not user_id or not practice_id:
        return False
    try:
        from bson import ObjectId
        res = db[COLL_WRITING_PRACTICE].delete_one({"_id": ObjectId(practice_id), "user_id": user_id})
        return res.deleted_count > 0
    except Exception:
        traceback.print_exc()
        return False
