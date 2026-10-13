# -*- coding: utf-8 -*-
"""
付費方案判斷（單一 Pro）。所有「免費版 / Pro 版」的差異只在這個檔案判斷，其他模組不要自己寫 if pro。

原則（見 docs/tcm-gamification-spec-v2.1.md 第 7 節）：
- 遊戲化核心（題庫、等級、圖鑑、合成、分享）全部免費。
- Pro 只解鎖「更多可以玩的關卡與練習次數」、完整食譜、外觀等；藥材與食材絕不付費取得。
- 數值（每日額度、免費關卡數…）來自 data/gm/config.json，這裡不寫死。
- 目前只預留結構，不串金流：Pro 由 set_plan() 手動設定（開發開關、demo 用）。
"""
from datetime import datetime, timezone

try:
    from api import gm_content as C
except ImportError:
    import gm_content as C


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def is_pro(doc, now=None):
    if (doc or {}).get("plan") != "pro":
        return False
    exp = _parse(doc.get("pro_expires_at"))
    if exp is None:
        return True
    return exp > (now or datetime.now(timezone.utc))


def dev_flag(doc, name):
    return bool(((doc or {}).get("dev") or {}).get(name))


def life_open_all(doc, now=None):
    """生活線 24 關是否全部開放（Pro 或開發開關）。"""
    return is_pro(doc, now) or dev_flag(doc, "open_all")


def life_free_levels_per_day(doc, now=None):
    """免費版每天可以挑戰的『新關卡』數；Pro 不限（回傳 None）。"""
    if life_open_all(doc, now):
        return None
    return int(C.config().get("life", {}).get("free_levels_per_day", 1))


def can_view_full_recipe(doc, now=None):
    return is_pro(doc, now)


def extra_practice_drop_rate(doc, now=None):
    """口說／寫作：當日第一次有效練習之外，Pro 的額外練習掉落機率；免費版沒有額外掉落。"""
    if is_pro(doc, now):
        return float(C.config().get("practice_drop", {}).get("extra_practice_rate", 0.5))
    return 0.0


def ai_daily_quota(doc, now=None):
    """AI 詳解／問答的每日額度（Phase 2 才會真正限制，這裡先預留）。Pro 回傳 None＝無限。"""
    if is_pro(doc, now):
        return None
    return int(C.config().get("quota", {}).get("free_ai_daily", 10))


def set_plan(doc, plan, days=None, now=None):
    """手動設定方案（開發開關、demo）。plan: 'free' | 'pro'；days 給了就設到期日。"""
    now = now or datetime.now(timezone.utc)
    if plan not in ("free", "pro"):
        raise ValueError("plan must be free or pro")
    doc["plan"] = plan
    if plan == "pro" and days:
        from datetime import timedelta
        doc["pro_expires_at"] = (now + timedelta(days=int(days))).isoformat()
    else:
        doc["pro_expires_at"] = None
