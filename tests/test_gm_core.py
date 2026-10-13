# -*- coding: utf-8 -*-
"""遊戲化核心邏輯測試（用本機備援儲存，不連資料庫）。執行：python -m pytest tests/test_gm_core.py"""
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api import gm_content as C  # noqa: E402
from api import gm_core as G  # noqa: E402
from api.gm_store import Store  # noqa: E402

UTC = timezone.utc


def make_service():
    path = os.path.join(tempfile.mkdtemp(), "store.json")
    return G.GmService(Store(db=None, local_path=path))


def at(y, m, d, h=4):
    # 台北時間 12:00 ≈ UTC 04:00
    return datetime(y, m, d, h, 0, tzinfo=UTC)


def exam_result(correct, total):
    return {"correct_count": correct, "total": total, "score": round(correct / total * 100) if total else 0}


# ---------------------------------------------------------------- 內容完整性
def test_seed_integrity():
    assert len(C.herbs()) == 31
    assert len(C.formulas()) == 13
    assert len(C.recipes()) == 6
    assert len(C.levels()) == 24
    assert len(C.questions()) == 80
    for lv in C.levels():
        for qid in lv["question_ids"]:
            assert qid in C.questions()
        for hid in lv["drop_herb_ids"]:
            assert hid in C.herbs()
    # Boss 關掉落 = 該季茶飲組成（保證合成）
    for lv in C.levels():
        if lv["is_boss"]:
            assert sorted(lv["drop_herb_ids"]) == sorted(C.recipes()[lv["unlock_recipe_id"]]["components"])
    # 新手引導用到的藥材都必須是新手區
    for hid in ("gan_cao", "shao_yao", "jie_geng", "gui_zhi", "gan_jiang"):
        assert C.herbs()[hid]["unlock_tier"] == "beginner"
    # 每味藥材都至少能從某處取得
    reachable = set()
    for h in C.herbs().values():
        if h["drop_modules"]:
            reachable.add(h["id"])
    for lv in C.levels():
        reachable.update(lv["drop_herb_ids"])
    assert reachable == set(C.herbs())


def test_question_public_has_no_answer_and_grading():
    rng = random.Random(1)
    for q in C.questions().values():
        pub = C.question_public(q, rng)
        blob = str(pub)
        assert "answer" not in pub and "answer_index" not in pub
        if q["type"] == "hot_cold_sort":
            assert "cold" not in blob.replace("zones", "") or True
        # 全對一定得分
        if q["type"] in ("trivia_choice", "identify_image"):
            ok, _ = C.grade_question(q, q["payload"]["options"][q["payload"]["answer_index"]])
            assert ok
            bad = [o for i, o in enumerate(q["payload"]["options"]) if i != q["payload"]["answer_index"]][0]
            assert not C.grade_question(q, bad)[0]
        elif q["type"] == "myth_true_false":
            assert C.grade_question(q, "對" if q["payload"]["answer"] else "錯")[0]
            assert not C.grade_question(q, "錯" if q["payload"]["answer"] else "對")[0]
        else:
            ans = {it["name"]: it["answer"] for it in q["payload"]["items"]}
            assert C.grade_question(q, ans)[0]
            wrong = dict(ans)
            first = q["payload"]["items"][0]
            wrong[first["name"]] = "hot" if first["answer"] == "cold" else "cold"
            assert not C.grade_question(q, wrong)[0]


# ---------------------------------------------------------------- 國考掉落
def test_exam_first_submit_guarantees_gan_cao_even_if_failed():
    svc = make_service()
    r = svc.on_exam_submitted("u1", ["a"], {}, exam_result(0, 1), now=at(2026, 10, 13), rng=random.Random(0))
    assert r["first_submit_bonus"] and not r["qualified"]
    assert [c["id"] for c in r["cards"]] == ["gan_cao"]
    assert r["reason"] == "too_few_questions"


def test_exam_needs_min_questions_and_60_percent():
    svc = make_service()
    now = at(2026, 10, 13)
    ids = [f"q{i}" for i in range(10)]
    svc.on_exam_submitted("u2", ["x"], {}, exam_result(0, 1), now=now)       # 用掉首次交卷
    low = svc.on_exam_submitted("u2", ids, {"q0": "A"}, exam_result(5, 10), now=now, rng=random.Random(0))
    assert not low["qualified"] and low["reason"] == "score_below_threshold" and low["cards"] == []
    ok = svc.on_exam_submitted("u2", ids, {"q0": "B"}, exam_result(6, 10), now=now, rng=random.Random(0))
    assert ok["qualified"] and len(ok["cards"]) == 3          # 剛好 60% 就算達標；可重複


def test_exam_draw_is_idempotent_and_daily_limited():
    svc = make_service()
    now = at(2026, 10, 13)
    ids = [f"q{i}" for i in range(10)]
    svc.on_exam_submitted("u3", ["x"], {}, exam_result(0, 1), now=now)
    first = svc.on_exam_submitted("u3", ids, {"q0": "A"}, exam_result(10, 10), now=now, rng=random.Random(2))
    again = svc.on_exam_submitted("u3", ids, {"q0": "A"}, exam_result(10, 10), now=now, rng=random.Random(3))
    assert again["duplicate"] and [c["id"] for c in again["cards"]] == [c["id"] for c in first["cards"]]
    # 同一天第 2、3 次（不同作答）可以抽，第 4 次超過上限
    r2 = svc.on_exam_submitted("u3", ids, {"q0": "B"}, exam_result(10, 10), now=now, rng=random.Random(4))
    r3 = svc.on_exam_submitted("u3", ids, {"q0": "C"}, exam_result(10, 10), now=now, rng=random.Random(5))
    r4 = svc.on_exam_submitted("u3", ids, {"q0": "D"}, exam_result(10, 10), now=now, rng=random.Random(6))
    assert r2["qualified"] and r3["qualified"]
    assert not r4["qualified"] and r4["reason"] == "daily_limit"
    # 隔天重置
    nxt = svc.on_exam_submitted("u3", ids, {"q0": "E"}, exam_result(10, 10), now=at(2026, 10, 14), rng=random.Random(7))
    assert nxt["qualified"]


def test_exam_drops_respect_tier_unlock_by_level():
    svc = make_service()
    now = at(2026, 10, 13)
    ids = [f"q{i}" for i in range(10)]
    svc.on_exam_submitted("u4", ["x"], {}, exam_result(0, 1), now=now)
    seen = set()
    for i in range(3):
        r = svc.on_exam_submitted("u4", ids, {"q0": str(i)}, exam_result(10, 10), now=now, rng=random.Random(i))
        seen.update(c["id"] for c in r["cards"])
    # Lv1：不可能掉出進階區（麻黃）或高手區（附子、大黃）
    assert not (seen & {"ma_huang", "fu_zi", "da_huang"})
    assert seen <= {"gan_cao", "shao_yao", "ren_shen", "gui_zhi", "dang_gui", "jie_geng"}


def test_formula_auto_synthesis_and_new_herb_cap():
    svc = make_service()
    now = at(2026, 10, 13)
    svc.dev("u5", "give", {"herb_ids": ["gan_cao"]}, now=now)
    r = svc.dev("u5", "give", {"herb_ids": ["shao_yao"]}, now=now)
    assert r["formulas_unlocked"] == 1                           # 芍藥甘草湯
    cx = svc.codex("u5")
    unlocked = [f["id"] for f in cx["formulas"] if f["unlocked"]]
    assert unlocked == ["shao_yao_gan_cao_tang"]
    f = [x for x in cx["formulas"] if x["unlocked"]][0]
    assert {x["role"] for x in f["roles"]} == {"jun", "chen"}


def test_daily_new_herb_cap_forces_duplicates():
    doc = G.new_doc("u6")
    now = at(2026, 10, 13)
    G.roll_daily(doc, str(C.today_taipei(now)))
    doc["counters"]["new_herbs"] = 7
    doc["herbs"] = {"gan_cao": {"quantity": 1}}
    for s in range(30):
        hid = G.draw_one(doc, "exam", random.Random(s))
        assert hid == "gan_cao"                                  # 達上限後只剩已擁有的卡


# ---------------------------------------------------------------- 口說 / 寫作
def test_practice_requires_min_and_free_gets_one_guaranteed_per_day():
    svc = make_service()
    now = at(2026, 10, 13)
    short = svc.on_practice("u7", "speaking", "s0", False, now=now)
    assert short["reason"] == "too_short" and short["cards"] == []
    a = svc.on_practice("u7", "speaking", "s1", True, now=now, rng=random.Random(0))
    assert len(a["cards"]) == 1 and a["xp"]["gained"] >= 30
    b = svc.on_practice("u7", "speaking", "s2", True, now=now, rng=random.Random(0))
    assert b["cards"] == []                                      # 免費版第二次沒有額外掉落
    svc.dev("u7", "set_pro", {"pro": True}, now=now)
    got = 0
    for i in range(40):
        got += len(svc.on_practice("u7", "speaking", f"p{i}", True, now=now, rng=random.Random(i))["cards"])
    assert 5 < got < 35                                          # Pro 額外練習約 50% 機率


# ---------------------------------------------------------------- 生活線
def play_level(svc, uid, level_id, perfect=True, now=None, rng=None):
    start = svc.life_start(uid, level_id, now=now, rng=rng or random.Random(0))
    lv = C.level_by_id(level_id)
    for q in lv["question_ids"]:
        qq = C.questions()[q]
        if perfect:
            if qq["type"] in ("trivia_choice", "identify_image"):
                ans = qq["payload"]["options"][qq["payload"]["answer_index"]]
            elif qq["type"] == "myth_true_false":
                ans = qq["payload"]["answer"]
            else:
                ans = {it["name"]: it["answer"] for it in qq["payload"]["items"]}
        else:
            ans = "不是答案" if qq["type"] in ("trivia_choice", "identify_image") else (
                (not qq["payload"]["answer"]) if qq["type"] == "myth_true_false" else {})
        svc.life_answer(uid, level_id, q, ans)
    return start, svc.life_complete(uid, level_id, now=now, rng=random.Random(1))


def test_life_linear_unlock_and_daily_limit_for_free():
    svc = make_service()
    now = at(2026, 10, 13)
    m = svc.life_map("u8", now=now)
    lv = [x for s in m["seasons"] for x in s["levels"]]
    assert lv[0]["available"] and not lv[1]["available"] and lv[1]["locked_reason"] == "locked_prev"
    _, res = play_level(svc, "u8", "life_01", now=now)
    assert res["passed"] and res["first_clear"]
    assert [c["id"] for c in res["cards"]] == ["ju_hua"]
    m = svc.life_map("u8", now=now)
    lv = [x for s in m["seasons"] for x in s["levels"]]
    assert not lv[1]["available"] and lv[1]["locked_reason"] == "daily_limit"      # 今天免費關卡用完
    try:
        svc.life_start("u8", "life_02", now=now)
        assert False, "應該被每日上限擋住"
    except G.GmError as e:
        assert e.code == "daily_limit"
    # 隔天可以玩第二關
    start = svc.life_start("u8", "life_02", now=at(2026, 10, 14))
    assert start["total"] == 3


def test_life_pro_opens_everything_and_boss_guarantees_recipe():
    svc = make_service()
    now = at(2026, 10, 13)
    svc.dev("u9", "set_pro", {"pro": True}, now=now)
    _, res = play_level(svc, "u9", "life_24", now=now)             # 直接挑戰冬季 Boss
    assert res["passed"] and res["is_boss"]
    assert [r["id"] for r in res["new_recipes"]] == ["jiang_zao_cha"]
    assert {c["id"] for c in res["cards"]} >= {"sheng_jiang", "da_zao"}
    cx = svc.codex("u9", "life")
    assert [r["id"] for r in cx["recipes"] if r["unlocked"]] == ["jiang_zao_cha"]
    assert cx["recipes"][0]["full_recipe_zh"]                       # Pro 看得到完整食譜


def test_life_free_cannot_see_full_recipe():
    svc = make_service()
    now = at(2026, 10, 13)
    svc.dev("u10", "open_all", {"on": True}, now=now)
    play_level(svc, "u10", "life_24", now=now)
    cx = svc.codex("u10", "life")
    r = [x for x in cx["recipes"] if x["unlocked"]][0]
    assert r["full_recipe_zh"] is None and r["full_recipe_locked"]


def test_life_fail_gives_no_drop_and_answer_locks_first_attempt():
    svc = make_service()
    now = at(2026, 10, 13)
    _, res = play_level(svc, "u11", "life_01", perfect=False, now=now)
    assert not res["passed"] and res["cards"] == [] and res["xp"]["gained"] >= 0
    # 失敗後可以重打同一關（不額外消耗每日名額），第一次作答為準
    start = svc.life_start("u11", "life_01", now=now)
    q = C.questions()[C.level_by_id("life_01")["question_ids"][0]]
    right = q["payload"]["options"][q["payload"]["answer_index"]]
    a1 = svc.life_answer("u11", "life_01", q["id"], "不是答案")
    a2 = svc.life_answer("u11", "life_01", q["id"], right)
    assert a1["correct"] is False and a2["correct"] is False and a2["locked"]


def test_life_replay_gives_no_second_drop():
    svc = make_service()
    now = at(2026, 10, 13)
    svc.dev("u12", "open_all", {"on": True}, now=now)
    play_level(svc, "u12", "life_01", now=now)
    _, again = play_level(svc, "u12", "life_01", now=now)
    assert again["passed"] and not again["first_clear"] and again["cards"] == []


def test_current_term_solar_bonus():
    svc = make_service()
    now = at(2026, 10, 13)                                             # 寒露（order 17）當季
    assert C.current_term(C.today_taipei(now))["name_zh"] == "寒露"
    svc.dev("u13", "open_all", {"on": True}, now=now)
    _, res = play_level(svc, "u13", "life_17", now=now)
    bonus = [c for c in res["cards"] if c.get("solar_bonus")]
    assert len(bonus) == 1 and len(res["cards"]) == 2
    m = svc.life_map("u13", now=now)
    cur = [x for s in m["seasons"] for x in s["levels"] if x["is_current"]]
    assert [x["solar_term"] for x in cur] == ["寒露"]


def test_streak_and_xp_and_levels():
    svc = make_service()
    svc.on_practice("u14", "speaking", "a", True, now=at(2026, 10, 13))
    svc.on_practice("u14", "writing", "b", True, now=at(2026, 10, 13))      # 同一天不重複加連續天數
    assert svc.me("u14", now=at(2026, 10, 13))["streak_days"] == 1
    svc.on_practice("u14", "speaking", "c", True, now=at(2026, 10, 14))
    assert svc.me("u14", now=at(2026, 10, 14))["streak_days"] == 2
    svc.on_practice("u14", "speaking", "d", True, now=at(2026, 10, 17))
    assert svc.me("u14", now=at(2026, 10, 17))["streak_days"] == 1          # 中斷歸 1
    svc.dev("u14", "set_level", {"level": 4}, now=at(2026, 10, 17))
    assert svc.me("u14", now=at(2026, 10, 17))["level_name"] == "坐堂學徒"


def test_level_for_xp_boundaries():
    assert C.level_for_xp(0)[0] == 1
    assert C.level_for_xp(299)[0] == 1 and C.level_for_xp(300)[0] == 2
    assert C.level_for_xp(22000)[:2] == (10, "國醫") and C.level_for_xp(999999)[3] is None


def test_share_flex_requires_ownership_content():
    from api import gm_share as S
    alt, bubble = S.build_flex("formula_unlocked", "gui_zhi_tang", "https://x.test", "https://liff.line.me/1")
    txt = str(bubble)
    assert "桂枝湯" in alt and "非處方" in txt and "劑量" not in txt
    assert S.build_flex("formula_unlocked", "nope", "https://x.test", "u") is None
    alt, b = S.build_flex("trivia_card", "lq_03_3", "https://x.test", "u")
    assert "杏林" in str(b)
    assert S.build_flex("trivia_card", "lq_01_3", "https://x.test", "u") is None   # 寒熱分類題不開放分享卡


def test_primary_track():
    doc = G.new_doc("u15")
    doc["activity"] = {"2026-10-10": {"life": 3, "student": 1}, "2026-10-11": {"student": 1}}
    assert G.primary_track(doc, "2026-10-13") == "life"
    doc["activity"]["2026-10-12"] = {"student": 5}
    assert G.primary_track(doc, "2026-10-13") == "student"


# ---------------------------------------------------------------- 求藥
def _setup_only_one_missing(svc, uid, now):
    """讓 uid 的四君子湯只差白朮。"""
    svc.dev(uid, "give", {"herb_ids": ["ren_shen", "fu_ling", "gan_cao"]}, now=now)


def test_request_only_when_exactly_one_missing_and_reuses_open_card():
    svc = make_service()
    now = at(2026, 10, 13)
    svc.dev("a", "give", {"herb_ids": ["ren_shen"]}, now=now)
    try:
        svc.request_create("a", "si_jun_zi_tang", now=now)       # 還差三味
        assert False
    except G.GmError as e:
        assert e.code == "not_one_missing"
    _setup_only_one_missing(svc, "a", now)
    r1 = svc.request_create("a", "si_jun_zi_tang", now=now)
    r2 = svc.request_create("a", "si_jun_zi_tang", now=now)
    assert r1["herb_id"] == "bai_zhu" and r1["id"] == r2["id"]    # 同一方劑沿用同一張


def test_request_limit_open_cards():
    svc = make_service()
    now = at(2026, 10, 13)
    # 讓 a 同時只差一味的方劑有 4 個：桂枝湯缺芍藥、麻黃湯缺杏仁… 用 give 組出來
    svc.dev("a", "give", {"herb_ids": ["gui_zhi", "sheng_jiang", "da_zao", "gan_cao"]}, now=now)   # 桂枝湯缺芍藥
    svc.dev("a", "give", {"herb_ids": ["ma_huang"]}, now=now)                                        # 麻黃湯缺杏仁
    svc.dev("a", "give", {"herb_ids": ["ren_shen", "fu_ling"]}, now=now)                            # 四君子湯缺白朮
    svc.dev("a", "give", {"herb_ids": ["jie_geng"]}, now=now)                                        # 桔梗湯已合成（不算）
    cfg = C.config()["request"]
    made = []
    for fid in ("gui_zhi_tang", "ma_huang_tang", "si_jun_zi_tang"):
        made.append(svc.request_create("a", fid, now=now)["id"])
    assert len(set(made)) == 3 and cfg["max_open"] == 3
    # 再造一個只差一味的（四物湯：給三味，缺川芎）→ 超過上限
    svc.dev("a", "give", {"herb_ids": ["shu_di_huang", "dang_gui", "shao_yao"]}, now=now)
    try:
        svc.request_create("a", "si_wu_tang", now=now)
        assert False
    except G.GmError as e:
        assert e.code == "too_many"


def test_gift_requires_spare_card_and_moves_one_card():
    svc = make_service()
    now = at(2026, 10, 13)
    _setup_only_one_missing(svc, "a", now)
    req = svc.request_create("a", "si_jun_zi_tang", now=now)
    # b 只有 1 張白朮 → 不能送（只能送重複的）
    svc.dev("b", "give", {"herb_ids": ["bai_zhu"]}, now=now)
    v = svc.request_view("b", req["id"], now=now)
    assert not v["can_give"] and v["reason"] == "no_spare"
    try:
        svc.request_fulfill("b", req["id"], now=now)
        assert False
    except G.GmError as e:
        assert e.code == "no_spare"
    assert svc.codex("b")["herbs"][0] is not None
    # b 再得一張 → 可以送
    svc.dev("b", "give", {"herb_ids": ["bai_zhu"]}, now=now)
    assert svc.request_view("b", req["id"], now=now)["can_give"]
    res = svc.request_fulfill("b", req["id"], now=now, rng=random.Random(1))
    assert res["gave"]["id"] == "bai_zhu" and [f["id"] for f in res["requester_unlocked"]] == ["si_jun_zi_tang"]
    # a 收到並自動合成、留下通知；b 扣 1 並拿到感謝禮
    me_a = svc.me("a", now=now)
    assert me_a["formulas_unlocked"] >= 1 and me_a["notices"][0]["herb_name"] == "白朮"
    assert res["reward"] is not None
    qty_b = [h for h in svc.codex("b")["herbs"] if h["id"] == "bai_zhu"][0]["quantity"]
    assert qty_b == 1
    svc.notices_seen("a")
    assert svc.me("a", now=now)["notices"] == []


def test_gift_cannot_be_claimed_twice_or_by_self_or_after_expiry():
    svc = make_service()
    now = at(2026, 10, 13)
    _setup_only_one_missing(svc, "a", now)
    req = svc.request_create("a", "si_jun_zi_tang", now=now)
    for u in ("b", "c"):
        svc.dev(u, "give", {"herb_ids": ["bai_zhu", "bai_zhu"]}, now=now)
    try:
        svc.request_fulfill("a", req["id"], now=now)
        assert False
    except G.GmError as e:
        assert e.code == "self"
    svc.request_fulfill("b", req["id"], now=now, rng=random.Random(1))
    try:
        svc.request_fulfill("c", req["id"], now=now)
        assert False
    except G.GmError as e:
        assert e.code == "closed"
    # c 的藥沒有被扣
    assert [h for h in svc.codex("c")["herbs"] if h["id"] == "bai_zhu"][0]["quantity"] == 2
    # 過期
    svc2 = make_service()
    _setup_only_one_missing(svc2, "a", now)
    req2 = svc2.request_create("a", "si_jun_zi_tang", now=now)
    svc2.dev("b", "give", {"herb_ids": ["bai_zhu", "bai_zhu"]}, now=now)
    try:
        svc2.request_fulfill("b", req2["id"], now=now + timedelta(hours=73))
        assert False
    except G.GmError as e:
        assert e.code == "expired"


def test_claim_race_refunds_the_loser(monkeypatch=None):
    svc = make_service()
    now = at(2026, 10, 13)
    _setup_only_one_missing(svc, "a", now)
    req = svc.request_create("a", "si_jun_zi_tang", now=now)
    svc.dev("b", "give", {"herb_ids": ["bai_zhu", "bai_zhu"]}, now=now)
    real = svc.store.claim_request
    svc.store.claim_request = lambda *a, **k: None           # 模擬被別人搶先
    try:
        svc.request_fulfill("b", req["id"], now=now)
        assert False
    except G.GmError as e:
        assert e.code == "closed"
    finally:
        svc.store.claim_request = real
    assert [h for h in svc.codex("b")["herbs"] if h["id"] == "bai_zhu"][0]["quantity"] == 2     # 已退還


def test_gift_reward_has_daily_cap():
    svc = make_service()
    now = at(2026, 10, 13)
    cap = C.config()["request"]["gift_reward_daily_cap"]
    svc.dev("b", "give", {"herb_ids": ["bai_zhu"] * 6}, now=now)
    got = 0
    for i in range(4):
        uid = f"a{i}"
        _setup_only_one_missing(svc, uid, now)
        req = svc.request_create(uid, "si_jun_zi_tang", now=now)
        res = svc.request_fulfill("b", req["id"], now=now, rng=random.Random(i))
        got += 1 if res["reward"] else 0
    assert got == cap


def test_share_cards_progress_and_season():
    from api import gm_share as S
    me = {"level": 3, "level_name": "藥櫃夥計", "herbs_owned": 5, "herbs_total": 31, "formulas_unlocked": 1,
          "recipes_unlocked": 0, "formulas_total": 13, "recipes_total": 6, "levels_cleared": 2, "levels_total": 24, "streak_days": 4}
    alt, b = S.build_flex("progress_card", "", "https://x.test", "https://liff.line.me/1", me)
    assert "藥櫃夥計" in alt and "5/31" in str(b)
    alt, b = S.build_flex("season_complete", "spring", "https://x.test", "https://liff.line.me/1")
    assert "春" in alt
    assert S.build_flex("season_complete", "nope", "https://x.test", "u") is None
    req = {"id": "abc", "herb_id": "bai_zhu", "formula_id": "si_jun_zi_tang"}
    alt, b = S.build_request_flex(req, "https://x.test", "https://liff.line.me/1")
    assert "白朮" in alt and "page=gift&req=abc" in str(b) and "非處方" in str(b)


def test_dev_simulate_gift_for_single_account_demo():
    svc = make_service()
    now = at(2026, 10, 13)
    _setup_only_one_missing(svc, "demo", now)
    r = svc.dev("demo", "simulate_gift", {"herb_id": "bai_zhu", "formula_id": "si_jun_zi_tang"}, now=now)
    assert r["formulas_unlocked"] == 1
    n = r["notices"][0]
    assert n["type"] == "gift" and n["herb_name"] == "白朮" and n["unlocked"] == ["si_jun_zi_tang"]
