# -*- coding: utf-8 -*-
"""
NPC 對話式口說教練：Realtime API session 設定、逐輪錯誤標註、結算頁摘要。

語音對話本身走 OpenAI Realtime API（瀏覽器端用 WebRTC 直接連線到 OpenAI，
不經過我們的伺服器中繼，延遲最低）。這裡只負責：
1. 幫兩種情境（臨床衛教／學術討論）組出對應的 Realtime session 設定與角色 instructions
2. 伺服器端用主 API Key 換一組短效 ephemeral client secret 給前端用（不可把主 Key 交給瀏覽器）
3. 逐輪／結算頁的錯誤標註分析——這兩塊是一般文字 chat.completions，跟 Realtime API 的語音對話是分開的

刻意不寫入 MongoDB／Redis：對話逐字稿只在這次瀏覽器 session 存在，
離開結算頁就清空（對應線框圖「口說練習資料只記錄到結束對話之前，離開結算頁就全部清空」）。
"""
import json
import traceback

SCENARIOS = {
    "clinical": {
        "label_zh": "臨床衛教",
        "character_zh": "患者",
        "voice": "shimmer",
        "instructions": (
            "You are role-playing as a patient visiting a Traditional Chinese Medicine (TCM) clinic, "
            "speaking with a TCM student in English. Stay fully in character as a patient — describe your "
            "symptoms in everyday language (not medical jargon), answer the student's history-taking "
            "questions naturally, and react like a real patient would. Occasionally ask the student "
            "questions back, especially about self-care advice, so they get a chance to practice patient "
            "education (health education) in English. Keep your turns short and conversational "
            "(1-3 sentences), speak at a clear, moderate pace suitable for a language learner, and always "
            "reply in English."
        ),
    },
    "academic": {
        "label_zh": "學術討論",
        "character_zh": "教授",
        "voice": "cedar",
        "instructions": (
            "You are role-playing as a TCM (Traditional Chinese Medicine) professor having an academic "
            "discussion in English with a TCM student, helping them practice expressing TCM concepts and "
            "clinical reasoning in English. Ask thoughtful follow-up questions, gently push the student to "
            "be more precise in their terminology and reasoning, and share your own insight when relevant. "
            "Keep your turns short and conversational (1-3 sentences), speak at a clear, moderate pace "
            "suitable for a language learner, and always reply in English."
        ),
    },
}

DEFAULT_SCENARIO = "clinical"
REALTIME_MODEL = "gpt-realtime"

# 提示框內容（MVP：固定內容，不用 AI 動態產生，簡單且省成本）
HINTS = {
    "clinical": {
        "topics": [
            "問對方哪裡不舒服、多久了 (chief complaint & duration)",
            "問誘發／緩解因素 (aggravating / relieving factors)",
            "簡單衛教一句話，例如飲食或作息建議",
        ],
        "vocab": [
            "chief complaint（主訴）", "aggravate（加重）", "relieve（緩解）",
            "dietary advice（飲食建議）", "follow up（回診追蹤）",
        ],
    },
    "academic": {
        "topics": [
            "請對方解釋一個中醫概念（如陰陽、氣血）給你聽",
            "討論某個證型的病機",
            "問這個概念在臨床上怎麼應用",
        ],
        "vocab": [
            "pattern differentiation（辨證）", "Qi and Blood（氣血）",
            "Yin-Yang theory（陰陽學說）", "pathogenesis（病機）", "clinical application（臨床應用）",
        ],
    },
}


def get_hints(scenario_key):
    return HINTS.get(scenario_key) or HINTS[DEFAULT_SCENARIO]


def get_scenario(scenario_key):
    return SCENARIOS.get(scenario_key) or SCENARIOS[DEFAULT_SCENARIO]


def list_scenarios():
    """給主畫面兩個主題按鈕用。"""
    return [
        {
            "key": key,
            "label_zh": s["label_zh"],
            "character_zh": s["character_zh"],
            "hints": get_hints(key),
        }
        for key, s in SCENARIOS.items()
    ]


def build_session_config(scenario_key):
    scenario = get_scenario(scenario_key)
    return {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "instructions": scenario["instructions"],
        "audio": {
            "output": {"voice": scenario["voice"]},
            "input": {
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {"type": "server_vad"},
            },
        },
    }


def mint_ephemeral_session(openai_client, scenario_key):
    """
    伺服器端呼叫，用主 API Key 換一組短效 ephemeral client secret（openai.realtime.client_secrets.create）。
    回傳的 client_secret 才是前端可以安全使用、拿去跟 OpenAI 建立 WebRTC 連線的值。
    失敗回傳 None，由呼叫端決定如何提示使用者。
    """
    scenario_key = scenario_key if scenario_key in SCENARIOS else DEFAULT_SCENARIO
    scenario = SCENARIOS[scenario_key]
    try:
        resp = openai_client.realtime.client_secrets.create(session=build_session_config(scenario_key))
        return {
            "client_secret": resp.value,
            "expires_at": resp.expires_at,
            "model": REALTIME_MODEL,
            "scenario": scenario_key,
            "character_zh": scenario["character_zh"],
            "label_zh": scenario["label_zh"],
        }
    except Exception:
        traceback.print_exc()
        return None


def translate_to_zh(openai_client, text):
    """
    角色台詞即時中譯（對話框雙語字幕用）。純翻譯，不做任何其他加工，失敗回傳空字串。
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "Translate the following English sentence into natural Traditional Chinese (繁體中文). "
                    "Return ONLY the translation, no explanation, no quotes."
                )},
                {"role": "user", "content": text[:500]},
            ],
            max_tokens=200,
            temperature=0,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        traceback.print_exc()
        return ""


def annotate_errors(openai_client, text):
    """
    逐輪即時錯誤標註：回傳原句，但錯誤片段用「«」「»」包住（前端轉成橘色標註），
    完全正確就原樣回傳、不加任何符號。純文字 chat.completions，跟語音對話分開跑，
    用意是在使用者說完一句話、Realtime API 轉出文字逐字稿後，立刻補一次語言／內容檢查。
    失敗時回傳原句（前端視為「沒有標註出錯誤」，不影響對話進行）。
    """
    text = (text or "").strip()
    if not text:
        return text
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "You proofread a TCM (Traditional Chinese Medicine) student's spoken English sentence "
                    "for (1) grammar/word-choice errors and (2) TCM clinical or academic content errors. "
                    "Return the EXACT same sentence, wrapping ONLY the erroneous word(s) or phrase(s) with "
                    "« and » (no other changes, no explanation, no extra text). "
                    "If there is no error at all, return the sentence unchanged with no « » marks."
                )},
                {"role": "user", "content": text[:500]},
            ],
            max_tokens=200,
            temperature=0,
        )
        return (resp.choices[0].message.content or text).strip()
    except Exception:
        traceback.print_exc()
        return text


def build_session_summary(openai_client, transcript):
    """
    結算頁摘要：transcript = list[{"role": "user"|"assistant", "text": str}]（前端傳整段對話逐字稿）。
    回傳 list[{"original": 含 «錯誤» 標記的原句, "corrected": 修正後版本}]，
    只回傳「有錯誤」的使用者發言（沒有錯誤的句子直接略過，符合線框圖「顯示...講錯的部分」）。
    """
    user_lines = [
        (t.get("text") or "").strip()
        for t in (transcript or [])
        if t.get("role") == "user" and (t.get("text") or "").strip()
    ]
    if not user_lines:
        return []
    joined = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(user_lines))
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "You review a TCM student's spoken English sentences from a practice conversation. "
                    "For each numbered sentence that contains a grammar, word-choice, or TCM content error, "
                    "output one JSON object with \"original\" (the sentence with the erroneous part(s) "
                    "wrapped in « and ») and \"corrected\" (a fluent corrected version). "
                    "Skip sentences with no error entirely. "
                    "Return ONLY a JSON array of such objects, no extra text, no markdown code fence."
                )},
                {"role": "user", "content": joined},
            ],
            max_tokens=1000,
            temperature=0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if "```" in raw:
            parts = raw.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("json"):
                    p = p[4:].strip()
                if p.startswith("["):
                    raw = p
                    break
        if "[" in raw and "]" in raw:
            raw = raw[raw.find("["): raw.rfind("]") + 1]
        arr = json.loads(raw)
        out = []
        for item in arr:
            orig = (item.get("original") or "").strip()
            corr = (item.get("corrected") or "").strip()
            if orig and corr:
                out.append({"original": orig[:500], "corrected": corr[:500]})
        return out
    except Exception:
        traceback.print_exc()
        return []
