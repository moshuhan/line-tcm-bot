# -*- coding: utf-8 -*-
"""
分享卡 Flex Message（Bubble）。給 LIFF shareTargetPicker 用，由後端組好、前端直接送出。

隱私規則（規格第 9 節）：分享卡只包含遊戲成就與知識內容，絕不帶出對話內容、症狀或個人健康資訊；
方劑卡不顯示劑量並附註「學習內容，非處方」；茶飲卡附註「非醫療建議」；文案不宣稱療效。
"""
try:
    from api import gm_content as C
except ImportError:
    import gm_content as C

BRAND = "岐黃 AI · 中藥圖鑑"
FORMULA_NOTE = "學習內容，非處方。請勿自行抓藥服用，身體不適請諮詢合格中醫師。"
RECIPE_NOTE = "傳統飲食文化內容，非醫療建議。孕婦、慢性病或正在服藥者，請先諮詢醫師。"
TRIVIA_NOTE = "傳統文化與飲食常識，非醫療建議。"

GOLD = "#B8860B"
INK = "#2B2B2B"
MUTE = "#8A8578"
CREAM = "#FBF3DC"
JADE = "#1F6F54"


def _text(t, **kw):
    d = {"type": "text", "text": t, "wrap": True}
    d.update(kw)
    return d


def _bubble(base_url, liff_url, title, lines, note, hero_state="celebrate", eyebrow=BRAND, button="我也要玩"):
    hero = {"type": "image", "url": f"{base_url}/assets/mascot/{hero_state}.webp", "size": "full",
            "aspectRatio": "1:1", "aspectMode": "fit", "backgroundColor": CREAM}
    body_contents = [_text(eyebrow, size="xs", color=GOLD, weight="bold"),
                     _text(title, size="lg", weight="bold", color=INK, margin="md")]
    for ln in lines:
        body_contents.append(_text(ln, size="sm", color=INK, margin="sm"))
    body_contents.append({"type": "separator", "margin": "lg"})
    body_contents.append(_text(note, size="xxs", color=MUTE, margin="md"))
    return {
        "type": "bubble", "size": "kilo", "hero": hero,
        "body": {"type": "box", "layout": "vertical", "contents": body_contents},
        "footer": {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "style": "primary", "color": JADE, "height": "sm",
             "action": {"type": "uri", "label": button, "uri": liff_url}}]},
    }


def build_flex(kind, ref_id, base_url, liff_url):
    """回傳 (altText, bubble) 或 None（找不到對應內容）。kind: formula_unlocked | recipe_unlocked | trivia_card。"""
    if kind == "formula_unlocked":
        f = C.formulas().get(ref_id)
        if not f:
            return None
        names = "、".join(C.herbs()[c["herb_id"]]["name_zh"] for c in f["components"])
        lines = [f"組成：{names}", f"出處：{f.get('source', '')}", f.get("function_summary_zh", "")]
        return (f"我合成了「{f['name_zh']}」！", _bubble(base_url, liff_url, f"我合成了{f['name_zh']}！", lines, FORMULA_NOTE))
    if kind == "recipe_unlocked":
        r = C.recipes().get(ref_id)
        if not r:
            return None
        names = "、".join(C.herbs()[h]["name_zh"] for h in r["components"])
        lines = [f"組成：{names}", r.get("traditional_use_zh", "")]
        return (f"我解鎖了「{r['name_zh']}」！", _bubble(base_url, liff_url, f"我解鎖了{r['name_zh']}！", lines, RECIPE_NOTE))
    if kind == "trivia_card":
        q = C.questions().get(ref_id)
        if not q or q["type"] not in ("trivia_choice", "myth_true_false") or not q.get("is_shareable", True):
            return None
        _, reveal = C.grade_question(q, None)
        ans = reveal.get("correct")
        if q["type"] == "myth_true_false":
            ans = "對" if ans else "錯"
            head = "迷思真假"
        else:
            head = "節氣冷知識"
        lines = [f"Q：{q['prompt_zh']}", f"答：{ans}", q["explanation_zh"]]
        return (f"{head}：{q['prompt_zh'][:30]}", _bubble(base_url, liff_url, head, lines, TRIVIA_NOTE,
                                                          hero_state="thinking", button="我也要挑戰"))
    return None
