# -*- coding: utf-8 -*-
"""
Evaluator（規格書第九節）：負責英文文法／TCM／Medical English／流暢度分析，
以及依對話狀態動態產生 hint、結算頁摘要。

刻意跟 Case/Topic Generator、Patient/Professor Agent、Session Manager 完全
獨立——這裡只吃「這一輪的文字」跟「這個 case/topic 的期望主題清單」，不知道
語音怎麼來的、session 怎麼存的、抽到的是哪個病例。之後 evaluation core
（規則引擎＋NER/RE）要接進來取代這裡的 GPT 分析時，只需要改這個檔案，
speaking_liff.py／speaking_session.py 完全不用動。
"""
import json
import traceback

_TURN_ANALYSIS_PROMPT = """You are analyzing one turn of a TCM (Traditional Chinese Medicine) student's English speaking practice conversation.

You will be given:
- The AI character's line (already spoken to the student)
- The student's line (what the student just said in English)
- A list of "expected topics" for this case/topic that the student is meant to explore during the conversation

Produce a single JSON object with exactly these fields:
{
  "translation": "Traditional Chinese (繁體中文) translation of the AI character's line",
  "language_feedback": {
    "has_error": true or false,
    "annotated_original": "the student's line, EXACTLY as given, with only the erroneous word(s)/phrase(s) wrapped in « and » — if no error, return it unchanged with no marks",
    "corrected_sentence": "a minimally corrected version fixing only actual errors, or empty string if no error",
    "natural_sentence": "a more natural/fluent way a native speaker might phrase the same idea, or empty string if the original is already natural"
  },
  "terminology": ["any TCM or medical English terms relevant to this turn, empty array if none"],
  "hint": {
    "suggestion": "one short, concrete suggestion for what the student could ask or say next, based on which expected topics have NOT been covered yet",
    "vocabulary": ["1-3 useful English words/phrases for the suggested next step"],
    "sentence_starter": "a short sentence starter the STUDENT could say next, addressed to the AI character, e.g. 'Have you noticed...' or 'Could you explain...'"
  },
  "newly_covered_topics": ["any expected topics from the provided list that THIS turn (AI line + student line together) actually touched on — copy the exact topic string from the list, empty array if none"]
}

Return ONLY the JSON object, no markdown code fence, no extra text."""

_TURN_FALLBACK = {
    "translation": "",
    "language_feedback": {"has_error": False, "annotated_original": "", "corrected_sentence": "", "natural_sentence": ""},
    "terminology": [],
    "hint": {"suggestion": "", "vocabulary": [], "sentence_starter": ""},
    "newly_covered_topics": [],
}


def _extract_json_object(raw):
    raw = (raw or "").strip()
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                raw = part
                break
    if "{" in raw and "}" in raw:
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
    return json.loads(raw)


def analyze_turn(openai_client, assistant_text, user_text, expected_topics=None):
    """
    分析一輪對話。回傳 dict：
    {translation, language_feedback, terminology, hint, newly_covered_topics}
    失敗時回傳安全的預設值（欄位皆為空），不讓對話因為這支分析失敗而卡住。
    """
    fallback = dict(_TURN_FALLBACK)
    fallback["language_feedback"] = dict(_TURN_FALLBACK["language_feedback"])
    fallback["language_feedback"]["annotated_original"] = user_text or ""
    fallback["hint"] = dict(_TURN_FALLBACK["hint"])

    user_payload = json.dumps({
        "ai_line": assistant_text or "",
        "student_line": user_text or "",
        "expected_topics": expected_topics or [],
    }, ensure_ascii=False)

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _TURN_ANALYSIS_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            max_tokens=600,
            temperature=0,
        )
        parsed = _extract_json_object(resp.choices[0].message.content)
    except Exception:
        traceback.print_exc()
        return fallback

    return {
        "translation": (parsed.get("translation") or "").strip(),
        "language_feedback": parsed.get("language_feedback") or fallback["language_feedback"],
        "terminology": parsed.get("terminology") or [],
        "hint": parsed.get("hint") or fallback["hint"],
        "newly_covered_topics": parsed.get("newly_covered_topics") or [],
    }


_SESSION_FALLBACK = {
    "clinical_precision": {"score": 0, "label": ""},
    "lexicon_accuracy": {"score": 0, "label": ""},
    "cases": [],
}


def summarize_session(openai_client, transcript):
    """
    結算頁摘要：transcript = list[{"role": "user"|"assistant", "text": str}]。
    回傳 {
      "clinical_precision": {"score": 0-100, "label": 簡短程度標籤，例如 "B2+ Competent"},
      "lexicon_accuracy": {"score": 0-100, "label": 簡短標籤，例如 "WHO-IST"},
      "cases": [{"category": 這句修正屬於哪一類, "original": 含 «錯誤» 標記的原句,
                 "corrected": 修正後版本, "explanation": 一兩句中文講解為什麼要這樣改}]
    }
    這兩個分數是 AI 讀完整場逐字稿後的整體評估（像老師評閱），不是真的語音學測量——
    語音走 Realtime API 的 WebRTC 直連，我們的伺服器拿不到錄音，做不到真正的音素發音
    分析，所以這裡刻意不做「發音準確度」這類需要聽錄音才能判斷的項目。
    """
    user_lines = [
        (t.get("text") or "").strip()
        for t in (transcript or [])
        if t.get("role") == "user" and (t.get("text") or "").strip()
    ]
    if not user_lines:
        return dict(_SESSION_FALLBACK)
    joined = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(user_lines))
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "You review a TCM (Traditional Chinese Medicine) student's spoken English sentences "
                    "from a practice conversation (patient consultation or academic discussion). "
                    "Return ONLY a JSON object, no markdown code fence, no extra text:\n"
                    "{\n"
                    '  "clinical_precision": {"score": 0-100 integer, holistic judgment of how clinically '
                    'accurate and appropriate the student\'s spoken TCM/medical reasoning was, '
                    '"label": "short CEFR-style competency label, e.g. \'B2+ Competent\'"},\n'
                    '  "lexicon_accuracy": {"score": 0-100 integer, how accurately the student used '
                    'standardized TCM/medical English terminology, "label": "short label, e.g. \'WHO-IST\'"},\n'
                    '  "cases": [{"category": "繁體中文，這句修正屬於哪一類，例如「中醫病機術語修正」、'
                    '「句法瑕疵」、「詞意不周」", "original": "the sentence with only the erroneous part(s) '
                    'wrapped in « and »", "corrected": "a fluent corrected version", '
                    '"explanation": "繁體中文，一兩句話說明為什麼要這樣修改"}]\n'
                    "}\n"
                    "Only include sentences that actually contain a grammar, word-choice, or TCM content "
                    "error in \"cases\" — skip sentences with no error entirely. If there are no numbered "
                    "sentences with errors, \"cases\" should be an empty array, but still return honest "
                    "clinical_precision/lexicon_accuracy scores based on the overall transcript."
                )},
                {"role": "user", "content": joined},
            ],
            max_tokens=1400,
            temperature=0,
            response_format={"type": "json_object"},
        )
        obj = _extract_json_object(resp.choices[0].message.content)
        cases = []
        for item in (obj.get("cases") or []):
            orig = (item.get("original") or "").strip()
            corr = (item.get("corrected") or "").strip()
            if orig and corr:
                cases.append({
                    "category": (item.get("category") or "").strip()[:60],
                    "original": orig[:500],
                    "corrected": corr[:500],
                    "explanation": (item.get("explanation") or "").strip()[:400],
                })
        cp = obj.get("clinical_precision") or {}
        la = obj.get("lexicon_accuracy") or {}
        return {
            "clinical_precision": {"score": int(cp.get("score", 0) or 0), "label": (cp.get("label") or "").strip()},
            "lexicon_accuracy": {"score": int(la.get("score", 0) or 0), "label": (la.get("label") or "").strip()},
            "cases": cases,
        }
    except Exception:
        traceback.print_exc()
        return dict(_SESSION_FALLBACK)
