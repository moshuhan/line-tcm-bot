# -*- coding: utf-8 -*-
"""
LIFF ID Token 驗證。

LIFF 前端用 liff.getIDToken() 拿到的 idToken 是使用者自己的瀏覽器產生的，
不可直接信任其宣稱的 userId——必須送到 LINE 官方端點驗證簽章與 client_id，
成功後才能拿到可信的 userId（對應到 Messaging API webhook 的同一組 user_id）。

需設定環境變數 LIFF_CHANNEL_ID：LIFF App 綁定的 LINE Login channel 的
Channel ID（在 LINE Developers Console 的 LIFF 分頁或 LINE Login 分頁可查到，
不是 LIFF ID 本身）。
"""
import os
import requests

LIFF_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"


def verify_liff_id_token(id_token):
    """
    驗證 LIFF ID Token。
    成功回傳 {"user_id": str, "raw": dict}；失敗（token 無效、逾期、未設定 channel）回傳 None。
    """
    channel_id = os.getenv("LIFF_CHANNEL_ID", "").strip()
    if not id_token or not channel_id:
        return None
    try:
        resp = requests.post(
            LIFF_VERIFY_URL,
            data={"id_token": id_token, "client_id": channel_id},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        user_id = (data.get("sub") or "").strip()
        if not user_id:
            return None
        return {"user_id": user_id, "raw": data}
    except Exception:
        return None
