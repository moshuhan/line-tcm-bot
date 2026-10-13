# -*- coding: utf-8 -*-
"""
遊戲化內容層（藥材圖鑑／方劑／茶飲／節氣關卡／題目）。

所有內容都放在 data/gm/*.json（種子資料，方便之後替換題目或補充藥材），這裡只負責：
1. 載入並建立索引；
2. 把題目轉成「不含答案」的前端版本、以及後端批改（答案永遠只在後端判斷）；
3. 依經驗值算等級。
設定值（等級表、掉落機率、每日上限…）全部放在 data/gm/config.json，不寫死在程式裡。
"""
import json
import os
import random
from datetime import date, datetime, timedelta, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "gm")

_cache = {}


def _load(name, default=None):
    path = os.path.join(DATA_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default if default is not None else []


def reload():
    """開發用：清掉快取，下次讀取時重新載入 JSON（改完 seed 檔不用重啟）。"""
    _cache.clear()


def _get(key, loader):
    if key not in _cache:
        _cache[key] = loader()
    return _cache[key]


def config():
    return _get("config", lambda: _load("config.json", {}))


def herbs():
    return _get("herbs", lambda: {h["id"]: h for h in _load("herbs.json")})


def formulas():
    return _get("formulas", lambda: {f["id"]: f for f in _load("formulas.json")})


def recipes():
    return _get("recipes", lambda: {r["id"]: r for r in _load("recipes.json")})


def evolution():
    return _get("evolution", lambda: _load("formula_evolution.json"))


def terms():
    return _get("terms", lambda: _load("solar_terms.json"))


def levels():
    return _get("levels", lambda: sorted(_load("life_levels.json"), key=lambda x: x["order"]))


def level_by_id(level_id):
    return _get("level_by_id", lambda: {lv["id"]: lv for lv in levels()}).get(level_id)


def questions():
    return _get("questions", lambda: {q["id"]: q for q in _load("life_questions.json")})


# ---------------------------------------------------------------- 等級
def level_for_xp(xp):
    """回傳 (level, name, 本級起點 xp, 下一級所需 xp 或 None)。"""
    table = sorted(config().get("levels", []), key=lambda x: x["xp"])
    cur = table[0] if table else {"lv": 1, "name": "", "xp": 0}
    nxt = None
    for i, row in enumerate(table):
        if xp >= row["xp"]:
            cur = row
            nxt = table[i + 1]["xp"] if i + 1 < len(table) else None
    return cur["lv"], cur["name"], cur["xp"], nxt


def min_level_for_tier(tier):
    return int(config().get("unlock_tier_min_level", {}).get(tier or "beginner", 1))


# ---------------------------------------------------------------- 節氣
TAIPEI = timezone(timedelta(hours=8))


def today_taipei(now=None):
    now = now or datetime.now(timezone.utc)
    return now.astimezone(TAIPEI).date()


def current_term(today):
    """回傳 today 當天所處的節氣（dict，含 order）：日期 <= today 的最後一個節氣。"""
    best = None
    best_d = None
    for t in terms():
        for ds in t["dates"].values():
            d = date.fromisoformat(ds)
            if d <= today and (best_d is None or d > best_d):
                best, best_d = t, d
    return best


def term_date_near(term, today):
    """顯示用：離今天最近的那一年的節氣日期。"""
    best = None
    for ds in term["dates"].values():
        d = date.fromisoformat(ds)
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


# ---------------------------------------------------------------- 題目（前端版本不含答案）
def _clean_item_names(q):
    return [it["name"] for it in q["payload"]["items"]]


def question_public(q, rng=None):
    """轉成前端用的題目：不含答案，選項順序隨機（正解不會永遠在同一個位置）。"""
    rng = rng or random
    t = q["type"]
    base = {"id": q["id"], "type": t, "prompt": q["prompt_zh"]}
    p = q["payload"]
    if t in ("trivia_choice", "identify_image"):
        opts = list(p["options"])
        rng.shuffle(opts)
        base["options"] = opts
        if t == "identify_image":
            base["image"] = p.get("image")
    elif t == "myth_true_false":
        base["options"] = ["對", "錯"]
    elif t == "hot_cold_sort":
        names = _clean_item_names(q)
        rng.shuffle(names)
        base["zones"] = p["zones"]
        base["items"] = names
    return base


def grade_question(q, answer):
    """
    後端批改。回傳 (is_correct, reveal)：reveal 是答完才給前端的正解資訊。
    答案格式：trivia/identify = 選項文字；myth = true/false 或 "對"/"錯"；hot_cold = {名稱: "cold"|"hot"}。
    """
    t = q["type"]
    p = q["payload"]
    if t in ("trivia_choice", "identify_image"):
        correct = p["options"][p["answer_index"]]
        return (answer == correct), {"correct": correct}
    if t == "myth_true_false":
        if isinstance(answer, str):
            answer = {"對": True, "錯": False, "true": True, "false": False}.get(answer.strip().lower(), None)
        return (answer is p["answer"]), {"correct": bool(p["answer"])}
    if t == "hot_cold_sort":
        given = answer if isinstance(answer, dict) else {}
        per = {}
        for it in p["items"]:
            per[it["name"]] = (given.get(it["name"]) == it["answer"])
        reveal = {"items": [{"name": it["name"], "answer": it["answer"]} for it in p["items"]], "per_item": per}
        return all(per.values()) and len(per) > 0, reveal
    return False, {}


def herb_brief(h):
    """卡片最小資訊（掉落、清單用）。"""
    return {"id": h["id"], "name_zh": h["name_zh"], "name_en": h.get("name_en"), "emoji": h.get("emoji", "🌿"),
            "image": h.get("image"), "tracks": h.get("tracks", [])}
