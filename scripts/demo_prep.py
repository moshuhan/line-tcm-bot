# -*- coding: utf-8 -*-
"""
單支手機、單一帳號錄 demo 的準備腳本（在你的電腦上執行，會呼叫 Railway 上的開發開關 API）。

事前（Railway 環境變數，錄影才暫時加，錄完移除）：
    GM_DEMO_MODE=1
    GM_DEV_SECRET=<自己想一組長密碼>
然後用手機打開生活線 LIFF →「我的」頁最下方會顯示「測試帳號 ID」（Uxxxxxxxx…），按住複製。

用法（PowerShell 或 bash 都可以）：
    set GM_DEV_SECRET=你的密碼                     # bash 用 export
    python scripts/demo_prep.py prep    --user Uxxxxxxxx    # 錄影前：重置帳號、備好「四君子湯只差白朮」、開啟必掉模式
    python scripts/demo_prep.py gift    --user Uxxxxxxxx    # 錄到「求藥」之後執行：模擬朋友送來一味白朮
    python scripts/demo_prep.py cleanup --user Uxxxxxxxx    # 錄完：關掉必掉模式

可用 --base 指定網址（預設是 Railway 正式站）。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "https://line-tcm-bot-production.up.railway.app"


def call(base, secret, user, action, params=None):
    req = urllib.request.Request(
        f"{base}/api/gm/dev/{action}",
        data=json.dumps({"user_id": user, "params": params or {}}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-GM-Dev-Secret": secret}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[失敗] {action}: HTTP {e.code} {e.read().decode('utf-8', 'ignore')[:200]}")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["prep", "gift", "cleanup"])
    ap.add_argument("--user", required=True, help="測試帳號的 LINE userId（Uxxxx…，在 LIFF「我的」頁最下方）")
    ap.add_argument("--base", default=os.getenv("GM_BASE", DEFAULT_BASE))
    a = ap.parse_args()
    secret = os.getenv("GM_DEV_SECRET", "").strip()
    if not secret:
        print("請先設定環境變數 GM_DEV_SECRET（要與 Railway 上的一致）")
        sys.exit(1)
    base = a.base.rstrip("/")

    if a.step == "prep":
        print("1/5 重置帳號"); call(base, secret, a.user, "reset")
        print("2/5 備好材料：人參、茯苓、甘草（四君子湯只差白朮）")
        call(base, secret, a.user, "give", {"herb_ids": ["ren_shen", "fu_ling", "gan_cao"]})
        print("3/5 開啟必掉模式（國考交卷、闖關一定掉藥；略過 60% 與每日上限）")
        call(base, secret, a.user, "demo_drops", {"on": True})
        print("4/5 開放全部節氣關卡")
        call(base, secret, a.user, "open_all", {"on": True})
        print("5/5 清除今日計數"); r = call(base, secret, a.user, "reset_daily")
        print(f"完成。目前：Lv{r['level']}、持有 {r['herbs_owned']} 味藥材、方劑 {r['formulas_unlocked']} 個")
        print("提醒：reset 之後帳號是全新的，第一次國考交卷會多掉一張甘草（首次交卷獎勵）。")
    elif a.step == "gift":
        r = call(base, secret, a.user, "simulate_gift", {"herb_id": "bai_zhu", "formula_id": "si_jun_zi_tang"})
        print(f"已模擬朋友送來一味白朮。現在持有 {r['herbs_owned']} 味、方劑 {r['formulas_unlocked']} 個。")
        print("手機回到首頁（下拉重新整理或重開）就會看到「朋友送來了禮物」，四君子湯已合成。")
    else:
        call(base, secret, a.user, "demo_drops", {"on": False})
        call(base, secret, a.user, "open_all", {"on": False})
        print("已關閉必掉模式與全關開放。記得到 Railway 移除 GM_DEMO_MODE 與 GM_DEV_SECRET。")


if __name__ == "__main__":
    main()
