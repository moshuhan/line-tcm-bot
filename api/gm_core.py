# -*- coding: utf-8 -*-
"""
遊戲化核心邏輯：XP／等級／連續天數、掉落、方劑與茶飲合成、生活線節氣闖關。

設計重點（見 docs/tcm-gamification-spec-v2.1.md）：
- **所有抽獎、批改、發獎都在後端**，前端只負責顯示結果。
- 國考掉落掛在「交卷之後」結算：題數達門檻且得分達標才抽卡；首次交卷必掉甘草。
- 口說／寫作：有效練習（達最低完成條件）才算；免費版每日第一次保證掉落，Pro 額外練習有機率掉落。
- 每筆結算都有冪等 key，網路重送不會重複發獎。
- 數值全部來自 data/gm/config.json。
純函式（操作傳進來的 doc），資料存取由 gm_store.Store 負責；GmService 把兩者接起來。
"""
import hashlib
import json
import random
import secrets
from datetime import datetime, timedelta, timezone

try:
    from api import gm_content as C
    from api import entitlements as E
    from api.gm_store import Store
except ImportError:
    import gm_content as C
    import entitlements as E
    from gm_store import Store


class GmError(Exception):
    def __init__(self, code, message, http=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


def now_utc():
    return datetime.now(timezone.utc)


def _iso(now):
    return now.isoformat()


def _parse_iso(s):
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ------------------------------------------------------------------ 文件結構
def new_doc(uid):
    return {
        "user_id": uid, "_v": 0,
        "xp": 0, "level": 1,
        "streak_days": 0, "last_practice_date": None,
        "plan": "free", "pro_expires_at": None,
        "primary_track": None,
        "first_exam_submitted": False,
        "counters": {"date": None, "exam_drop_submissions": 0, "exam_xp": 0, "practice_xp": 0, "gift_rewards": 0, "new_herbs": 0,
                     "life_attempt_levels": [], "speaking_practices": 0, "writing_practices": 0},
        "herbs": {}, "formulas": {}, "recipes": {},
        "life": {"cleared": {}},
        "attempt": None,
        "settled": {},
        "notices": [],
        "activity": {},
        "dev": {},
        "created_at": _iso(now_utc()),
    }


def roll_daily(doc, today_s):
    c = doc.setdefault("counters", {})
    if c.get("date") != today_s:
        c.update({"date": today_s, "exam_drop_submissions": 0, "exam_xp": 0, "practice_xp": 0, "gift_rewards": 0, "new_herbs": 0,
                  "life_attempt_levels": [], "speaking_practices": 0, "writing_practices": 0})


def _owned(doc):
    return {h for h, e in (doc.get("herbs") or {}).items() if int(e.get("quantity", 0)) > 0}


# ------------------------------------------------------------------ XP / 等級 / 連續天數
def add_xp(doc, amount):
    before = int(doc.get("level", 1))
    doc["xp"] = int(doc.get("xp", 0)) + max(0, int(amount))
    lv, name, floor, nxt = C.level_for_xp(doc["xp"])
    doc["level"] = lv
    return {"gained": max(0, int(amount)), "total": doc["xp"], "level": lv, "level_name": name,
            "next_xp": nxt, "level_floor": floor, "leveled_up": lv > before, "level_before": before}


def touch_streak(doc, today_s, track):
    """當天第一次在任一線完成練習／關卡：連續天數 +1（隔天沒玩就歸 1），並給當日連續天數 XP。"""
    act = doc.setdefault("activity", {})
    day = act.setdefault(today_s, {})
    day[track] = int(day.get(track, 0)) + 1
    for k in sorted(act.keys())[:-45]:       # 只留最近 45 天
        act.pop(k, None)
    last = doc.get("last_practice_date")
    gained = 0
    if last == today_s:
        pass
    else:
        yday = (datetime.fromisoformat(today_s) - timedelta(days=1)).date().isoformat()
        doc["streak_days"] = int(doc.get("streak_days", 0)) + 1 if last == yday else 1
        doc["last_practice_date"] = today_s
        gained = int(C.config().get("xp", {}).get("streak_daily", 20))
    doc["primary_track"] = primary_track(doc, today_s)
    return gained


def primary_track(doc, today_s):
    """近 30 天哪一線的活動次數較多（沒有紀錄回傳 None）。"""
    since = (datetime.fromisoformat(today_s) - timedelta(days=30)).date().isoformat()
    s = l = 0
    for d, v in (doc.get("activity") or {}).items():
        if d >= since:
            s += int(v.get("student", 0))
            l += int(v.get("life", 0))
    if s == l == 0:
        return None
    return "student" if s >= l else "life"


# ------------------------------------------------------------------ 掉落
def _tier_ok(doc, herb):
    return int(doc.get("level", 1)) >= C.min_level_for_tier(herb.get("unlock_tier"))


def _cap_reached(doc):
    return int(doc["counters"].get("new_herbs", 0)) >= int(C.config().get("daily_distinct_new_herb_cap", 7))


def grant_herb(doc, herb_id, source, now):
    """發一張藥材／食材卡。回傳卡片資訊（含是否為新卡）。"""
    h = C.herbs().get(herb_id)
    if not h:
        raise GmError("unknown_herb", f"unknown herb {herb_id}")
    entry = doc["herbs"].get(herb_id)
    is_new = not entry or int(entry.get("quantity", 0)) <= 0
    if entry:
        entry["quantity"] = int(entry.get("quantity", 0)) + 1
        if is_new:
            entry["first_obtained_at"] = _iso(now)
            entry["source"] = source
    else:
        doc["herbs"][herb_id] = {"quantity": 1, "first_obtained_at": _iso(now), "source": source}
    if is_new:
        doc["counters"]["new_herbs"] = int(doc["counters"].get("new_herbs", 0)) + 1
    card = C.herb_brief(h)
    card.update({"is_new": is_new, "quantity": int(doc["herbs"][herb_id]["quantity"]), "source": source})
    return card


def _near_complete_herbs(doc):
    """『只差一味就能合成』的方劑所缺的那一味 → 抽到的權重加倍。"""
    owned = _owned(doc)
    out = set()
    for f in C.formulas().values():
        if f["id"] in doc["formulas"]:
            continue
        missing = [c["herb_id"] for c in f["components"] if c["herb_id"] not in owned]
        if len(missing) == 1:
            out.add(missing[0])
    return out


def draw_one(doc, module, rng, track=None):
    """
    從「該模組藥材池 ∩ 已解鎖區」加權抽 1 味。達每日新藥材上限後，只會抽到已擁有的（重複卡）。
    回傳 herb_id 或 None（沒有可抽的）。
    """
    pool = []
    for h in C.herbs().values():
        if module and module not in (h.get("drop_modules") or []):
            continue
        if track and track not in (h.get("tracks") or []):
            continue
        if not _tier_ok(doc, h):
            continue
        pool.append(h)
    if _cap_reached(doc) and not E.dev_flag(doc, "demo_drops"):
        owned = _owned(doc)
        pool = [h for h in pool if h["id"] in owned]
    if not pool:
        return None
    near = _near_complete_herbs(doc)
    mult = float(C.config().get("near_complete_weight_multiplier", 2))
    weights = [float(h.get("drop_weight", 1)) * (mult if h["id"] in near else 1.0) for h in pool]
    return rng.choices(pool, weights=weights, k=1)[0]["id"]


def check_synthesis(doc, now):
    """持有全部組成就自動解鎖方劑；『不限季節』的茶飲同理。季節茶飲只能靠 Boss 關合成。"""
    owned = _owned(doc)
    new_f, new_r = [], []
    for f in C.formulas().values():
        if f["id"] in doc["formulas"]:
            continue
        if all(c["herb_id"] in owned for c in f["components"]):
            doc["formulas"][f["id"]] = {"unlocked_at": _iso(now)}
            new_f.append(f["id"])
    for r in C.recipes().values():
        if r["id"] in doc["recipes"] or r.get("season") != "any":
            continue
        if all(h in owned for h in r["components"]):
            doc["recipes"][r["id"]] = {"unlocked_at": _iso(now)}
            new_r.append(r["id"])
    return new_f, new_r


def _formula_brief(fid):
    f = C.formulas()[fid]
    return {"id": fid, "name_zh": f["name_zh"], "name_en": f.get("name_en"), "source": f.get("source"),
            "image": f.get("image"), "kind": "formula"}


def _recipe_brief(rid):
    r = C.recipes()[rid]
    return {"id": rid, "name_zh": r["name_zh"], "name_en": r.get("name_en"), "emoji": r.get("emoji", "🍵"),
            "image": r.get("image"), "kind": "recipe"}


def _xp_block(doc, info):
    return {"gained": info["gained"], "total": info["total"], "level": info["level"],
            "level_name": info["level_name"], "next_xp": info["next_xp"], "leveled_up": info["leveled_up"]}


def _remember(doc, key, result, now):
    s = doc.setdefault("settled", {})
    s[key] = {"at": _iso(now), "result": result}
    if len(s) > 60:
        for k, _ in sorted(s.items(), key=lambda kv: kv[1].get("at", ""))[:len(s) - 60]:
            s.pop(k, None)


def _seen(doc, key, now, window_sec):
    prev = (doc.get("settled") or {}).get(key)
    if not prev:
        return None
    at = _parse_iso(prev.get("at"))
    if at is None or (now - at).total_seconds() > window_sec:
        return None
    res = dict(prev["result"])
    res["duplicate"] = True
    return res


def exam_key(user_id, question_ids, answers):
    """同一份考卷（同一位使用者、同一組題目與作答）的冪等 key。"""
    raw = json.dumps([user_id, sorted(question_ids or []), sorted((answers or {}).items())],
                     ensure_ascii=False, sort_keys=True)
    return "exam:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# ------------------------------------------------------------------ 國考交卷結算
def settle_exam(doc, key, correct, total, score, rng=None, now=None):
    """
    交卷（批改、寫入紀錄、寫入錯題本都已完成）之後呼叫。
    - XP、連續天數：交卷即給（不受 60% 門檻影響），國考 XP 有每日上限防止洗題。
    - 掉落：題數 >= min_questions 且得分 >= min_score，且今日可抽卡的交卷次數未滿，抽 draws 張（可重複）。
    - 首次交卷必掉甘草（不受上述條件限制）。
    """
    now = now or now_utc()
    rng = rng or random
    cfg = C.config()
    ed = cfg.get("exam_drop", {})
    today_s = str(C.today_taipei(now))
    roll_daily(doc, today_s)
    seen = _seen(doc, key, now, int(ed.get("dedupe_window_sec", 600)))
    if seen:
        return seen

    demo = E.dev_flag(doc, "demo_drops")
    result = {"qualified": False, "reason": None, "cards": [], "new_formulas": [], "new_recipes": [],
              "first_submit_bonus": False, "duplicate": False,
              "threshold": {"min_questions": ed.get("min_questions", 10), "min_score": ed.get("min_score", 60),
                            "draws": ed.get("draws", 3)}}

    # --- XP（答對 +10、答錯 +2；有每日上限）
    xcfg = cfg.get("xp", {})
    raw = int(correct) * int(xcfg.get("correct", 10)) + max(0, int(total) - int(correct)) * int(xcfg.get("wrong", 2))
    cap = int(cfg.get("exam_xp_daily_cap", 600))
    room = max(0, cap - int(doc["counters"].get("exam_xp", 0)))
    gain = min(raw, room)
    doc["counters"]["exam_xp"] = int(doc["counters"].get("exam_xp", 0)) + gain
    gain += touch_streak(doc, today_s, "student")
    xp_info = add_xp(doc, gain)

    # --- 首次交卷必掉甘草
    if not doc.get("first_exam_submitted"):
        doc["first_exam_submitted"] = True
        result["cards"].append(grant_herb(doc, ed.get("first_submit_herb", "gan_cao"), "exam", now))
        result["first_submit_bonus"] = True

    # --- 抽卡條件
    min_q = int(ed.get("min_questions", 10))
    min_s = float(ed.get("min_score", 60))
    per_day = int(ed.get("submissions_per_day", 3))
    if demo and total > 0:
        qualifies = True
    elif total < min_q:
        qualifies, result["reason"] = False, "too_few_questions"
    elif score < min_s:
        qualifies, result["reason"] = False, "score_below_threshold"
    elif int(doc["counters"].get("exam_drop_submissions", 0)) >= per_day:
        qualifies, result["reason"] = False, "daily_limit"
    else:
        qualifies = True
    if qualifies:
        result["qualified"] = True
        result["reason"] = None
        if not demo:
            doc["counters"]["exam_drop_submissions"] = int(doc["counters"].get("exam_drop_submissions", 0)) + 1
        for _ in range(int(ed.get("draws", 3))):
            hid = draw_one(doc, "exam", rng)
            if hid:
                result["cards"].append(grant_herb(doc, hid, "exam", now))

    new_f, new_r = check_synthesis(doc, now)
    result["new_formulas"] = [_formula_brief(i) for i in new_f]
    result["new_recipes"] = [_recipe_brief(i) for i in new_r]
    result["xp"] = _xp_block(doc, xp_info)
    result["streak_days"] = doc.get("streak_days", 0)
    _remember(doc, key, result, now)
    return result


# ------------------------------------------------------------------ 口說／寫作練習結算
def practice_key(kind, session_key):
    return f"{kind}:{hashlib.sha1(str(session_key).encode('utf-8')).hexdigest()[:20]}"


def settle_practice(doc, kind, key, valid, rng=None, now=None):
    """
    kind: 'speaking' | 'writing'。valid＝達到最低完成條件（口說輪數／寫作字詞數）。
    免費版每日第一次有效練習保證掉 1 張；之後 Pro 有機率（extra_practice_rate）掉 1 張，免費版沒有。
    """
    now = now or now_utc()
    rng = rng or random
    cfg = C.config()
    pc = cfg.get("practice_drop", {})
    today_s = str(C.today_taipei(now))
    roll_daily(doc, today_s)
    seen = _seen(doc, key, now, 3600)
    if seen:
        return seen
    result = {"kind": kind, "valid": bool(valid), "cards": [], "new_formulas": [], "new_recipes": [],
              "reason": None, "duplicate": False,
              "threshold": {"speaking_min_turns": pc.get("speaking_min_turns", 5),
                            "writing_min_words": pc.get("writing_min_words", 50)}}
    if not valid:
        result["reason"] = "too_short"
        return result
    # 練習 XP 有每日上限（避免反覆微調同一份內容洗經驗值）；連續天數獎勵不受影響
    pcap = int(cfg.get("practice_xp_daily_cap", 120))
    proom = max(0, pcap - int(doc["counters"].get("practice_xp", 0)))
    base = min(int(cfg.get("xp", {}).get(f"{kind}_practice", 30)), proom)
    doc["counters"]["practice_xp"] = int(doc["counters"].get("practice_xp", 0)) + base
    gain = base + touch_streak(doc, today_s, "student")
    xp_info = add_xp(doc, gain)
    ck = f"{kind}_practices"
    n_before = int(doc["counters"].get(ck, 0))
    doc["counters"][ck] = n_before + 1
    demo = E.dev_flag(doc, "demo_drops")
    drop = False
    if demo or n_before < int(pc.get("free_guaranteed_per_day", 1)):
        drop = True
    elif rng.random() < E.extra_practice_drop_rate(doc, now):
        drop = True
    if drop:
        hid = draw_one(doc, kind, rng)
        if hid:
            result["cards"].append(grant_herb(doc, hid, kind, now))
    new_f, new_r = check_synthesis(doc, now)
    result["new_formulas"] = [_formula_brief(i) for i in new_f]
    result["new_recipes"] = [_recipe_brief(i) for i in new_r]
    result["xp"] = _xp_block(doc, xp_info)
    result["streak_days"] = doc.get("streak_days", 0)
    _remember(doc, key, result, now)
    return result


# ------------------------------------------------------------------ 生活線：節氣闖關
def _cleared(doc):
    return (doc.get("life") or {}).get("cleared", {})


def _level_state(doc, lv, today_s, pro_or_open):
    """回傳 (available, locked_reason)。免費：線性解鎖＋每天最多 N 個『新』關卡；Pro／開發開關：全開。"""
    cleared = _cleared(doc)
    if lv["id"] in cleared:
        return True, ""
    if pro_or_open:
        return True, ""
    if lv["order"] > 1:
        prev = C.levels()[lv["order"] - 2]
        if prev["id"] not in cleared:
            return False, "locked_prev"
    attempted = doc["counters"].get("life_attempt_levels", [])
    limit = int(C.config().get("life", {}).get("free_levels_per_day", 1))
    if lv["id"] not in attempted and len(attempted) >= limit:
        return False, "daily_limit"
    return True, ""


def life_map(doc, now=None):
    now = now or now_utc()
    today = C.today_taipei(now)
    today_s = str(today)
    roll_daily(doc, today_s)
    open_all = E.life_open_all(doc, now)
    cur = C.current_term(today)
    cur_order = cur["order"] if cur else None
    terms_by_order = {t["order"]: t for t in C.terms()}
    cleared = _cleared(doc)
    out = []
    for lv in C.levels():
        avail, reason = _level_state(doc, lv, today_s, open_all)
        t = terms_by_order.get(lv["order"])
        item = {"id": lv["id"], "order": lv["order"], "solar_term": lv["solar_term"], "season": lv["season"],
                "is_boss": lv["is_boss"], "cleared": lv["id"] in cleared,
                "best_score": (cleared.get(lv["id"]) or {}).get("best_score"),
                "available": avail, "locked_reason": reason, "is_current": lv["order"] == cur_order,
                "term_date": str(C.term_date_near(t, today)) if t else None,
                "question_count": len(lv["question_ids"]), "pass_threshold": lv["pass_threshold"]}
        if lv.get("unlock_recipe_id"):
            item["recipe"] = _recipe_brief(lv["unlock_recipe_id"])
        out.append(item)
    seasons = []
    for s in ("spring", "summer", "autumn", "winter"):
        lvs = [x for x in out if x["season"] == s]
        seasons.append({"season": s, "levels": lvs, "cleared": sum(1 for x in lvs if x["cleared"]), "total": len(lvs)})
    attempted = doc["counters"].get("life_attempt_levels", [])
    return {"seasons": seasons, "current_term": ({"order": cur["order"], "name_zh": cur["name_zh"],
                                                  "name_en": cur["name_en"], "season": cur["season"]} if cur else None),
            "plan": "pro" if E.is_pro(doc, now) else "free", "open_all": bool(open_all),
            "free_levels_per_day": E.life_free_levels_per_day(doc, now), "attempted_today": len(attempted)}


def life_start(doc, level_id, now=None, rng=None):
    now = now or now_utc()
    rng = rng or random
    lv = C.level_by_id(level_id)
    if not lv:
        raise GmError("no_level", "找不到這個關卡", 404)
    today_s = str(C.today_taipei(now))
    roll_daily(doc, today_s)
    avail, reason = _level_state(doc, lv, today_s, E.life_open_all(doc, now))
    if not avail:
        msg = "請先通過上一關" if reason == "locked_prev" else "今天的免費關卡已經玩完囉，明天再來（Pro 不限關數）"
        raise GmError(reason or "locked", msg, 403)
    attempted = doc["counters"].setdefault("life_attempt_levels", [])
    if lv["id"] not in _cleared(doc) and lv["id"] not in attempted:
        attempted.append(lv["id"])
    qs = [C.questions()[q] for q in lv["question_ids"]]
    doc["attempt"] = {"level_id": lv["id"], "started_at": _iso(now), "qids": [q["id"] for q in qs], "answers": {}}
    return {"level": {"id": lv["id"], "order": lv["order"], "solar_term": lv["solar_term"], "season": lv["season"],
                      "is_boss": lv["is_boss"], "pass_threshold": lv["pass_threshold"]},
            "questions": [C.question_public(q, rng) for q in qs], "total": len(qs)}


def life_answer(doc, level_id, qid, answer):
    att = doc.get("attempt")
    if not att or att.get("level_id") != level_id or qid not in att.get("qids", []):
        raise GmError("no_attempt", "請重新開始這一關", 400)
    q = C.questions()[qid]
    prev = att["answers"].get(qid)
    if prev is not None:      # 每題只認第一次作答，避免先看答案再重答
        ok, reveal = C.grade_question(q, prev["given"])
        return {"correct": ok, "reveal": reveal, "explanation": q["explanation_zh"], "locked": True,
                "mascot": "happy" if ok else "ouch", "type": q["type"], "shareable": q.get("is_shareable", False),
                "question_id": qid}
    ok, reveal = C.grade_question(q, answer)
    att["answers"][qid] = {"given": answer, "correct": ok}
    return {"correct": ok, "reveal": reveal, "explanation": q["explanation_zh"], "locked": False,
            "mascot": "happy" if ok else "ouch", "type": q["type"], "shareable": q.get("is_shareable", False),
            "question_id": qid}


def life_complete(doc, level_id, now=None, rng=None):
    now = now or now_utc()
    rng = rng or random
    cfg = C.config()
    att = doc.get("attempt")
    lv = C.level_by_id(level_id)
    if not lv or not att or att.get("level_id") != level_id:
        raise GmError("no_attempt", "請重新開始這一關", 400)
    if len(att["answers"]) < len(att["qids"]):
        raise GmError("unanswered", "還有題目沒作答", 400)
    today = C.today_taipei(now)
    today_s = str(today)
    roll_daily(doc, today_s)
    n_total = len(att["qids"])
    n_correct = sum(1 for a in att["answers"].values() if a["correct"])
    passed = n_correct >= lv["pass_threshold"]
    cleared = _cleared(doc)
    first_clear = passed and lv["id"] not in cleared
    season_before = sum(1 for x in C.levels() if x["season"] == lv["season"] and x["id"] in cleared)

    gain = touch_streak(doc, today_s, "life")
    cards, new_recipes = [], []
    if passed:
        entry = cleared.get(lv["id"]) or {"cleared_at": _iso(now), "best_score": 0, "plays": 0}
        entry["best_score"] = max(int(entry.get("best_score", 0)), n_correct)
        entry["plays"] = int(entry.get("plays", 0)) + 1
        cleared[lv["id"]] = entry
        doc["life"]["cleared"] = cleared
        if first_clear:
            gain += int(cfg.get("xp", {}).get("life_clear", 20))
            # 指定掉落：保證發放（不受每日新藥材上限擋住，否則整個闖關進度會被國考抽卡卡死）
            for hid in lv["drop_herb_ids"]:
                cards.append(grant_herb(doc, hid, "life", now))
            # 當季加成：當前節氣那一關，首次過關多掉 1 張隨機生活線食材（受每日上限）
            bonus = int(cfg.get("life", {}).get("solar_bonus_extra_drops", 0))
            cur = C.current_term(today)
            if bonus and cur and cur["order"] == lv["order"]:
                for _ in range(bonus):
                    hid = draw_one(doc, None, rng, track="life")
                    if hid:
                        c = grant_herb(doc, hid, "life", now)
                        c["solar_bonus"] = True
                        cards.append(c)
        # Boss 關：保證合成該季茶飲（缺的組成補發，再解鎖）
        rid = lv.get("unlock_recipe_id")
        if rid and rid not in doc["recipes"]:
            owned = _owned(doc)
            for hid in C.recipes()[rid]["components"]:
                if hid not in owned:
                    cards.append(grant_herb(doc, hid, "life", now))
            doc["recipes"][rid] = {"unlocked_at": _iso(now)}
            new_recipes.append(rid)
    xp_info = add_xp(doc, gain)
    new_f, new_r = check_synthesis(doc, now)
    new_recipes += [r for r in new_r if r not in new_recipes]
    season_after = sum(1 for x in C.levels() if x["season"] == lv["season"] and x["id"] in _cleared(doc))
    total_season = sum(1 for x in C.levels() if x["season"] == lv["season"])
    doc["attempt"] = None
    return {"level_id": lv["id"], "is_boss": lv["is_boss"], "passed": passed, "correct": n_correct, "total": n_total,
            "threshold": lv["pass_threshold"], "first_clear": first_clear, "cards": cards,
            "new_recipes": [_recipe_brief(i) for i in new_recipes],
            "new_formulas": [_formula_brief(i) for i in new_f],
            "season_complete": bool(passed and season_after == total_season and season_before < total_season),
            "season": lv["season"], "xp": _xp_block(doc, xp_info), "streak_days": doc.get("streak_days", 0),
            "mascot": ("celebrate" if (new_recipes or (passed and lv["is_boss"])) else ("happy" if passed else "ouch"))}


# ------------------------------------------------------------------ 查詢：進度 / 圖鑑
def me_summary(doc, now=None):
    now = now or now_utc()
    today_s = str(C.today_taipei(now))
    roll_daily(doc, today_s)
    lv, name, floor, nxt = C.level_for_xp(doc.get("xp", 0))
    owned = _owned(doc)
    return {"user_id_short": doc["user_id"][-4:], "xp": doc.get("xp", 0), "level": lv, "level_name": name,
            "level_floor": floor, "next_xp": nxt, "streak_days": doc.get("streak_days", 0),
            "streak_active_today": doc.get("last_practice_date") == today_s,
            "plan": "pro" if E.is_pro(doc, now) else "free", "primary_track": doc.get("primary_track"),
            "herbs_owned": len(owned), "herbs_total": len(C.herbs()),
            "formulas_unlocked": len(doc.get("formulas", {})), "formulas_total": len(C.formulas()),
            "recipes_unlocked": len(doc.get("recipes", {})), "recipes_total": len(C.recipes()),
            "levels_cleared": len(_cleared(doc)), "levels_total": len(C.levels()),
            "new_herbs_today": int(doc["counters"].get("new_herbs", 0)),
            "new_herbs_cap": int(C.config().get("daily_distinct_new_herb_cap", 7)),
            "notices": [n for n in (doc.get("notices") or []) if not n.get("seen")],
            "dev": {k: v for k, v in (doc.get("dev") or {}).items() if v}}


def _herb_detail(h):
    return {"nature": h.get("nature"), "flavor": h.get("flavor"), "meridians": h.get("meridians"),
            "name_pinyin": h.get("name_pinyin"), "name_latin": h.get("name_latin"),
            "function_summary_zh": h.get("function_summary_zh"), "function_summary_en": h.get("function_summary_en"),
            "shennong_grade": h.get("shennong_grade"), "is_toxic": h.get("is_toxic", False),
            "toxicity_note": h.get("toxicity_note", "")}


def codex(doc, track=None, now=None):
    now = now or now_utc()
    owned = doc.get("herbs", {})
    herbs = []
    for h in C.herbs().values():
        if track and track not in h.get("tracks", []):
            continue
        e = owned.get(h["id"])
        qty = int(e.get("quantity", 0)) if e else 0
        item = {**C.herb_brief(h), "owned": qty > 0, "quantity": qty, "tier": h.get("unlock_tier"),
                "tier_locked": not _tier_ok(doc, h), "tier_min_level": C.min_level_for_tier(h.get("unlock_tier")),
                "name_pinyin": h.get("name_pinyin")}
        if qty > 0:
            item["detail"] = _herb_detail(h)
        herbs.append(item)
    herbs.sort(key=lambda x: (not x["owned"], x["tier_locked"], x["name_zh"]))
    ownedset = _owned(doc)
    formulas = []
    if track in (None, "student"):
        for f in C.formulas().values():
            unlocked = f["id"] in doc.get("formulas", {})
            comps = [{"herb_id": c["herb_id"], "name_zh": C.herbs()[c["herb_id"]]["name_zh"],
                      "emoji": C.herbs()[c["herb_id"]].get("emoji", "🌿"), "image": C.herbs()[c["herb_id"]].get("image"),
                      "owned": c["herb_id"] in ownedset} for c in f["components"]]
            if f.get("is_hidden") and not unlocked:
                continue
            item = {"id": f["id"], "name_zh": f["name_zh"] if unlocked else "？？？", "unlocked": unlocked,
                    "progress": sum(1 for c in comps if c["owned"]), "total": len(comps), "components": comps,
                    "category": f.get("category"), "kind": "formula"}
            if unlocked:
                item.update({"name_zh": f["name_zh"], "name_en": f.get("name_en"), "source": f.get("source"),
                             "function_summary_zh": f.get("function_summary_zh"),
                             "function_summary_en": f.get("function_summary_en"), "image": f.get("image"),
                             "roles": [{"herb_id": c["herb_id"], "name_zh": C.herbs()[c["herb_id"]]["name_zh"],
                                        "role": c.get("role"), "role_explanation": c.get("role_explanation")}
                                       for c in f["components"]]})
            else:
                item["name_zh"] = f["name_zh"]   # 顯示名稱讓使用者知道要收集什麼；細節解鎖後才給
            formulas.append(item)
        formulas.sort(key=lambda x: (not x["unlocked"], -(x["progress"] / max(1, x["total"])), x["name_zh"]))
    recipes = []
    if track in (None, "life"):
        pro = E.can_view_full_recipe(doc, now)
        for r in C.recipes().values():
            unlocked = r["id"] in doc.get("recipes", {})
            comps = [{"herb_id": h, "name_zh": C.herbs()[h]["name_zh"], "emoji": C.herbs()[h].get("emoji", "🌿"),
                      "image": C.herbs()[h].get("image"), "owned": h in ownedset} for h in r["components"]]
            item = {"id": r["id"], "name_zh": r["name_zh"], "season": r.get("season"), "unlocked": unlocked,
                    "progress": sum(1 for c in comps if c["owned"]), "total": len(comps), "components": comps,
                    "emoji": r.get("emoji", "🍵"), "kind": "recipe", "image": r.get("image")}
            if unlocked:
                item.update({"name_en": r.get("name_en"), "traditional_use_zh": r.get("traditional_use_zh"),
                             "caution_zh": r.get("caution_zh"),
                             "full_recipe_zh": r.get("full_recipe_zh") if pro else None, "full_recipe_locked": not pro})
            recipes.append(item)
        recipes.sort(key=lambda x: (not x["unlocked"], x["name_zh"]))
    return {"herbs": herbs, "formulas": formulas, "recipes": recipes,
            "totals": {"herbs_owned": sum(1 for h in herbs if h["owned"]), "herbs": len(herbs),
                       "formulas_unlocked": sum(1 for f in formulas if f["unlocked"]), "formulas": len(formulas),
                       "recipes_unlocked": sum(1 for r in recipes if r["unlocked"]), "recipes": len(recipes)},
            "plan": "pro" if E.is_pro(doc, now) else "free",
            "evolution": [e for e in C.evolution() if e["from_formula_id"] in doc.get("formulas", {})
                          or e["to_formula_id"] in doc.get("formulas", {})]}


# ------------------------------------------------------------------ 開發開關
def dev_apply(doc, action, params, now=None):
    """demo／測試用的開發開關。呼叫端（gm_routes）必須先驗證密鑰與白名單。"""
    now = now or now_utc()
    params = params or {}
    if action == "set_pro":
        E.set_plan(doc, "pro" if params.get("pro", True) else "free", params.get("days"), now)
    elif action == "open_all":
        doc.setdefault("dev", {})["open_all"] = bool(params.get("on", True))
    elif action == "demo_drops":
        doc.setdefault("dev", {})["demo_drops"] = bool(params.get("on", True))
    elif action == "give":
        ids = params.get("herb_ids") or ([params["herb_id"]] if params.get("herb_id") else [])
        for hid in ids:
            grant_herb(doc, hid, "admin", now)
        check_synthesis(doc, now)
    elif action == "set_level":
        lv = int(params.get("level", 1))
        row = next((r for r in C.config().get("levels", []) if r["lv"] == lv), None)
        if not row:
            raise GmError("bad_level", "level 必須是 1–10")
        doc["xp"] = row["xp"]
        add_xp(doc, 0)
    elif action == "add_xp":
        add_xp(doc, int(params.get("xp", 0)))
    elif action == "simulate_gift":
        # 單帳號錄 demo 用：模擬朋友把藥送過來（正式流程是 GmService.request_fulfill，這裡只是跳過另一位使用者）
        hid = params.get("herb_id") or "bai_zhu"
        if hid not in C.herbs():
            raise GmError("bad_herb", "unknown herb")
        roll_daily(doc, str(C.today_taipei(now)))
        grant_herb(doc, hid, "gift", now)
        new_f, _ = check_synthesis(doc, now)
        doc.setdefault("notices", []).append({
            "id": "demo-" + secrets.token_urlsafe(4), "type": "gift", "herb_id": hid,
            "herb_name": C.herbs()[hid]["name_zh"], "formula_id": params.get("formula_id"),
            "unlocked": new_f, "at": _iso(now), "seen": False})
    elif action == "reset_daily":
        doc["counters"] = new_doc(doc["user_id"])["counters"]
        doc["last_practice_date"] = None
    else:
        raise GmError("bad_action", f"unknown action {action}")
    return me_summary(doc, now)


# ------------------------------------------------------------------ 服務（接 Store）
class GmService:
    def __init__(self, store: Store):
        self.store = store
        self.store.ensure_index()

    # 共用：載入（唯讀）
    def _read(self, uid):
        return self.store.read(uid, new_doc)

    def _update(self, uid, fn):
        return self.store.update(uid, fn, new_doc)

    def me(self, uid, now=None):
        return self._update(uid, lambda d: me_summary(d, now))

    def codex(self, uid, track=None, now=None):
        return codex(self._read(uid), track, now)

    def life_map(self, uid, now=None):
        return self._update(uid, lambda d: life_map(d, now))

    def life_start(self, uid, level_id, now=None, rng=None):
        return self._update(uid, lambda d: life_start(d, level_id, now, rng))

    def life_answer(self, uid, level_id, qid, answer):
        return self._update(uid, lambda d: life_answer(d, level_id, qid, answer))

    def life_complete(self, uid, level_id, now=None, rng=None):
        return self._update(uid, lambda d: life_complete(d, level_id, now, rng))

    def on_exam_submitted(self, uid, question_ids, answers, result, now=None, rng=None):
        total = int(result.get("total") or 0)
        correct = int(result.get("correct_count") or 0)
        score = float(result.get("score") or 0)
        key = exam_key(uid, question_ids, answers)
        return self._update(uid, lambda d: settle_exam(d, key, correct, total, score, rng, now))

    def on_practice(self, uid, kind, session_key, valid, now=None, rng=None):
        key = practice_key(kind, session_key)
        return self._update(uid, lambda d: settle_practice(d, kind, key, valid, rng, now))

    def dev(self, uid, action, params=None, now=None):
        if action == "reset":
            self.store.reset_user(uid)
            return {"reset": True}
        return self._update(uid, lambda d: dev_apply(d, action, params, now))

    # ------------------------------------------------------------ 求藥（向朋友求一味藥）
    def _expired(self, req, now):
        exp = _parse_iso(req.get("expires_at"))
        return bool(exp and now > exp)

    def request_create(self, uid, formula_id, now=None):
        """方劑「只差一味」時發一張求藥卡（72 小時內有效）。同一方劑已有未過期的卡就直接沿用。"""
        now = now or now_utc()
        cfg = C.config().get("request", {})
        f = C.formulas().get(formula_id)
        if not f:
            raise GmError("no_formula", "找不到這個方劑", 404)
        doc = self._read(uid)
        if formula_id in (doc.get("formulas") or {}):
            raise GmError("already_unlocked", "這個方劑你已經合成了")
        owned = _owned(doc)
        missing = [c["herb_id"] for c in f["components"] if c["herb_id"] not in owned]
        if len(missing) != 1:
            raise GmError("not_one_missing", "只差一味藥的時候才能向朋友求藥")
        herb_id = missing[0]
        opens = []
        for r in self.store.list_requests(uid, "open"):
            if self._expired(r, now):
                self.store.update_request(r["id"], {"status": "expired"})
            else:
                opens.append(r)
        for r in opens:
            if r["herb_id"] == herb_id and r["formula_id"] == formula_id:
                return r
        if len(opens) >= int(cfg.get("max_open", 3)):
            raise GmError("too_many", "你已經有好幾張求藥卡在等朋友回應了，先等等吧", 429)
        req = {"id": secrets.token_urlsafe(8), "requester": uid, "herb_id": herb_id, "formula_id": formula_id,
               "status": "open", "created_at": _iso(now),
               "expires_at": _iso(now + timedelta(hours=int(cfg.get("expire_hours", 72)))),
               "fulfilled_by": None}
        self.store.create_request(req)
        return req

    def request_view(self, uid, req_id, now=None):
        """朋友點進求藥卡時看到的資訊：誰要什麼藥、你有沒有多的可以送。不洩漏對方的任何個人資料。"""
        now = now or now_utc()
        req = self.store.get_request(req_id)
        if not req:
            raise GmError("no_request", "找不到這張求藥卡", 404)
        status = req["status"]
        if status == "open" and self._expired(req, now):
            status = "expired"
        herb = C.herbs()[req["herb_id"]]
        f = C.formulas()[req["formula_id"]]
        doc = self._read(uid)
        qty = int(((doc.get("herbs") or {}).get(req["herb_id"]) or {}).get("quantity", 0))
        is_mine = req["requester"] == uid
        if status == "open" and not is_mine:
            rdoc = self._read(req["requester"])
            if req["herb_id"] in _owned(rdoc):
                status = "already_owned"
        can_give = status == "open" and not is_mine and qty >= 2
        reason = None
        if not can_give:
            reason = "mine" if is_mine else (status if status != "open" else "no_spare")
        return {"id": req_id, "status": status, "is_mine": is_mine, "herb": C.herb_brief(herb),
                "formula": {"id": f["id"], "name_zh": f["name_zh"]}, "you_have": qty, "can_give": can_give,
                "reason": reason, "expires_at": req["expires_at"]}

    def request_fulfill(self, uid, req_id, now=None, rng=None):
        """朋友送藥：只能送自己『重複的』那張。送出的人扣 1、求藥的人得 1，送的人另得一張感謝禮（每日有上限）。"""
        now = now or now_utc()
        rng = rng or random
        req = self.store.get_request(req_id)
        if not req:
            raise GmError("no_request", "找不到這張求藥卡", 404)
        if req["requester"] == uid:
            raise GmError("self", "這是你自己的求藥卡，要請朋友來送喔")
        if req["status"] != "open":
            raise GmError("closed", "這張求藥卡已經被其他朋友幫忙了")
        if self._expired(req, now):
            self.store.update_request(req_id, {"status": "expired"})
            raise GmError("expired", "這張求藥卡已經過期了")
        herb_id = req["herb_id"]
        rdoc = self._read(req["requester"])
        if herb_id in _owned(rdoc):
            self.store.update_request(req_id, {"status": "closed_owned"})
            raise GmError("already_owned", "對方已經有這味藥了，謝謝你的心意")

        def give(d):
            e = (d.get("herbs") or {}).get(herb_id)
            if not e or int(e.get("quantity", 0)) < 2:
                raise GmError("no_spare", "你沒有多的這味藥可以送（只能送重複的）")
            e["quantity"] = int(e["quantity"]) - 1
            return True

        def refund(d):
            d["herbs"][herb_id]["quantity"] = int(d["herbs"].get(herb_id, {}).get("quantity", 0)) + 1
            return True

        self._update(uid, give)                                   # 1. 先扣送藥人的（沒有多的就直接失敗，什麼都沒動）
        claimed = self.store.claim_request(req_id, {"fulfilled_by": uid, "fulfilled_at": _iso(now)})
        if not claimed:                                           # 2. 原子地搶下這張求藥卡；搶輸了就退還
            self._update(uid, refund)
            raise GmError("closed", "這張求藥卡剛剛被其他朋友幫忙了")

        def receive(d):                                           # 3. 求藥的人收到藥、自動合成、留一則通知
            roll_daily(d, str(C.today_taipei(now)))
            grant_herb(d, herb_id, "gift", now)
            new_f, new_r = check_synthesis(d, now)
            d.setdefault("notices", []).append({
                "id": req_id, "type": "gift", "herb_id": herb_id, "herb_name": C.herbs()[herb_id]["name_zh"],
                "formula_id": req["formula_id"], "unlocked": new_f, "at": _iso(now), "seen": False})
            d["notices"] = d["notices"][-20:]
            return new_f

        try:
            requester_new_f = self._update(req["requester"], receive)
        except Exception:
            self.store.update_request(req_id, {"status": "open", "fulfilled_by": None})
            self._update(uid, refund)
            raise

        cap = int(C.config().get("request", {}).get("gift_reward_daily_cap", 2))

        def reward(d):                                            # 4. 送藥人的感謝禮（隨機一張，每日上限）
            roll_daily(d, str(C.today_taipei(now)))
            card = None
            if int(d["counters"].get("gift_rewards", 0)) < cap:
                d["counters"]["gift_rewards"] = int(d["counters"].get("gift_rewards", 0)) + 1
                hid = draw_one(d, None, rng)
                if hid:
                    card = grant_herb(d, hid, "gift_reward", now)
            new_f, _ = check_synthesis(d, now)
            return card, new_f

        card, my_new_f = self._update(uid, reward)
        try:
            self.store.log_event({"user_id": uid, "type": "gift", "ref_id": req_id, "herb_id": herb_id,
                                  "to": req["requester"], "at": _iso(now)})
        except Exception:
            pass
        return {"ok": True, "gave": C.herb_brief(C.herbs()[herb_id]), "reward": card,
                "your_new_formulas": [_formula_brief(i) for i in my_new_f],
                "requester_unlocked": [_formula_brief(i) for i in requester_new_f]}

    def notices_seen(self, uid):
        def f(d):
            for n in d.get("notices") or []:
                n["seen"] = True
            return True
        return self._update(uid, f)

    def season_done(self, uid, season):
        d = self._read(uid)
        cleared = _cleared(d)
        lvs = [x for x in C.levels() if x["season"] == season]
        return bool(lvs) and all(x["id"] in cleared for x in lvs)

    def owns(self, uid, kind, ref_id):
        d = self._read(uid)
        return ref_id in (d.get("formulas" if kind == "formula" else "recipes") or {})
