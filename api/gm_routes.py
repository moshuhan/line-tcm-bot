# -*- coding: utf-8 -*-
"""
遊戲化的 Flask 路由與掛接點（Blueprint）。由 api/index.py 呼叫 register() 掛上去。

- 生活線 LIFF：GET /liff/life（單一 LIFF app，內部頁面切換），API 在 /api/liff/life/*、/api/liff/gm/*
- 靜態圖：/assets/mascot/*、/assets/herbs/*
- 開發開關（demo 用）：POST /api/gm/dev/<action>，需 GM_DEMO_MODE=1 ＋ 密鑰，且可用 GM_DEV_USER_IDS 限制可操作的帳號
- 掛接點 GmHooks：國考交卷、口說結算、寫作批改完成之後呼叫；任何例外都吞掉，絕不影響原本功能。
"""
import hashlib
import os
import re
import traceback

from flask import Blueprint, Response, jsonify, request, send_from_directory

try:
    from api import gm_content as C
    from api import gm_core as G
    from api import gm_share as S
    from api.gm_store import Store
except ImportError:
    import gm_content as C
    import gm_core as G
    import gm_share as S
    from gm_store import Store

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_ASSETS = os.path.join(_ROOT, "assets")
_TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")

LIFE_LIFF_ID = os.getenv("GM_LIFE_LIFF_ID", "2011870208-kD6N9oKn")


def _public_base(req):
    base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base
    root = req.url_root.rstrip("/")
    if root.startswith("http://") and "localhost" not in root and "127.0.0.1" not in root:
        root = "https://" + root[len("http://"):]
    return root


class GmHooks:
    """給現有功能（國考交卷、口說、寫作）呼叫的掛接點。回傳要附在 API 回應上的 dict，失敗回傳 None。"""

    def __init__(self, service):
        self.service = service

    def exam(self, user_id, question_ids, answers, result):
        try:
            return self.service.on_exam_submitted(user_id, question_ids, answers, result)
        except Exception:
            traceback.print_exc()
            return None

    def speaking(self, user_id, transcript, session_id=None):
        try:
            turns = sum(1 for t in (transcript or []) if isinstance(t, dict) and t.get("role") == "user"
                        and str(t.get("text") or "").strip())
            need = int(C.config().get("practice_drop", {}).get("speaking_min_turns", 5))
            key = session_id or hashlib.sha1(repr(transcript).encode("utf-8")).hexdigest()
            return self.service.on_practice(user_id, "speaking", key, turns >= need)
        except Exception:
            traceback.print_exc()
            return None

    def writing(self, user_id, text):
        try:
            words = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’-]*", text or ""))
            need = int(C.config().get("practice_drop", {}).get("writing_min_words", 50))
            return self.service.on_practice(user_id, "writing", text or "", words >= need)
        except Exception:
            traceback.print_exc()
            return None


def register(app, get_user_id, db):
    """掛上遊戲化路由。get_user_id：驗證 LIFF ID Token 並回傳 user_id（失敗 None）；db：MongoDB 或 None。"""
    store = Store(db=db)
    service = G.GmService(store)
    bp = Blueprint("gm", __name__)
    if not store.is_mongo:
        print(">>> GM: MongoDB 未連線，遊戲化資料使用本機備援儲存（僅限本機測試）：", store.local_path)

    def _auth():
        uid = get_user_id()
        if not uid:
            return None, (jsonify({"error": "unauthorized"}), 401)
        return uid, None

    def _gm_error(e):
        return jsonify({"error": e.message, "code": e.code}), e.http

    # -------------------------------------------------------------- 頁面與靜態圖
    @bp.route("/liff/life", methods=["GET"])
    def liff_life_page():
        try:
            with open(os.path.join(_TEMPLATES, "liff_life.html"), "r", encoding="utf-8") as f:
                html = f.read()
            html = html.replace("__LIFE_LIFF_ID__", LIFE_LIFF_ID)
            html = html.replace("__LIFF_DEBUG_TOKEN__", os.getenv("LIFF_DEBUG_TOKEN", "").strip())
            return Response(html, mimetype="text/html")
        except Exception:
            traceback.print_exc()
            return "LIFF page not found", 404

    def _static(subdir, filename):
        resp = send_from_directory(os.path.join(_ASSETS, subdir), filename)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @bp.route("/assets/gm/<path:filename>", methods=["GET"])
    def asset_gm(filename):
        """共用前端腳本（獎勵區塊）。.js 會把 __LIFE_LIFF_ID__ 換成實際的生活線 LIFF ID。"""
        from werkzeug.utils import safe_join
        full = safe_join(os.path.join(_ASSETS, "gm"), filename)
        if not full or not os.path.isfile(full):
            return "not found", 404
        if filename.endswith(".js"):
            with open(full, "r", encoding="utf-8") as f:
                body = f.read().replace("__LIFE_LIFF_ID__", LIFE_LIFF_ID)
            resp = Response(body, mimetype="application/javascript")
            resp.headers["Cache-Control"] = "public, max-age=300"
            return resp
        return _static("gm", filename)

    @bp.route("/assets/mascot/<path:filename>", methods=["GET"])
    def asset_mascot(filename):
        return _static("mascot", filename)

    @bp.route("/assets/herbs/<path:filename>", methods=["GET"])
    def asset_herbs(filename):
        return _static("herbs", filename)

    @bp.route("/assets/formulas/<path:filename>", methods=["GET"])
    def asset_formulas(filename):
        return _static("formulas", filename)

    @bp.route("/assets/recipes/<path:filename>", methods=["GET"])
    def asset_recipes(filename):
        return _static("recipes", filename)

    # -------------------------------------------------------------- 進度與圖鑑
    @bp.route("/api/liff/gm/me", methods=["GET"])
    def gm_me():
        uid, err = _auth()
        if err:
            return err
        return jsonify(service.me(uid))

    @bp.route("/api/liff/gm/codex", methods=["GET"])
    def gm_codex():
        uid, err = _auth()
        if err:
            return err
        track = (request.args.get("track") or "").strip() or None
        if track not in (None, "student", "life"):
            return jsonify({"error": "track 必須是 student 或 life"}), 400
        return jsonify(service.codex(uid, track))

    # -------------------------------------------------------------- 生活線
    @bp.route("/api/liff/life/map", methods=["GET"])
    def life_map():
        uid, err = _auth()
        if err:
            return err
        return jsonify(service.life_map(uid))

    @bp.route("/api/liff/life/level/start", methods=["POST"])
    def life_start():
        uid, err = _auth()
        if err:
            return err
        data = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(service.life_start(uid, (data.get("level_id") or "").strip()))
        except G.GmError as e:
            return _gm_error(e)

    @bp.route("/api/liff/life/level/answer", methods=["POST"])
    def life_answer():
        uid, err = _auth()
        if err:
            return err
        data = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(service.life_answer(uid, (data.get("level_id") or "").strip(),
                                               (data.get("question_id") or "").strip(), data.get("answer")))
        except G.GmError as e:
            return _gm_error(e)

    @bp.route("/api/liff/life/level/complete", methods=["POST"])
    def life_complete():
        uid, err = _auth()
        if err:
            return err
        data = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(service.life_complete(uid, (data.get("level_id") or "").strip()))
        except G.GmError as e:
            return _gm_error(e)

    # -------------------------------------------------------------- 分享
    @bp.route("/api/liff/gm/share-card", methods=["GET"])
    def gm_share_card():
        uid, err = _auth()
        if err:
            return err
        kind = (request.args.get("type") or "").strip()
        ref_id = (request.args.get("id") or "").strip()
        if kind in ("formula_unlocked", "recipe_unlocked"):
            if not service.owns(uid, "formula" if kind == "formula_unlocked" else "recipe", ref_id):
                return jsonify({"error": "尚未解鎖，無法分享"}), 403
        ctx = None
        if kind == "progress_card":
            ctx = service.me(uid)
        elif kind == "season_complete" and not service.season_done(uid, ref_id):
            return jsonify({"error": "這一季還沒全部通關，無法分享"}), 403
        built = S.build_flex(kind, ref_id, _public_base(request), f"https://liff.line.me/{LIFE_LIFF_ID}", ctx)
        if not built:
            return jsonify({"error": "找不到可分享的內容"}), 404
        alt, bubble = built
        return jsonify({"message": {"type": "flex", "altText": alt, "contents": bubble}})

    @bp.route("/api/liff/gm/share-event", methods=["POST"])
    def gm_share_event():
        uid, err = _auth()
        if err:
            return err
        data = request.get_json(force=True, silent=True) or {}
        store.log_event({"user_id": uid, "type": "share", "share_type": (data.get("share_type") or "")[:40],
                         "ref_id": (data.get("ref_id") or "")[:60], "target_platform": "line",
                         "status": (data.get("status") or "")[:20], "at": G.now_utc().isoformat()})
        return jsonify({"ok": True})

    # -------------------------------------------------------------- 求藥（向朋友求一味藥）
    @bp.route("/api/liff/gm/request", methods=["POST"])
    def gm_request_create():
        uid, err = _auth()
        if err:
            return err
        data = request.get_json(force=True, silent=True) or {}
        try:
            req = service.request_create(uid, (data.get("formula_id") or "").strip())
        except G.GmError as e:
            return _gm_error(e)
        built = S.build_request_flex(req, _public_base(request), f"https://liff.line.me/{LIFE_LIFF_ID}")
        if not built:
            return jsonify({"error": "無法產生求藥卡"}), 500
        alt, bubble = built
        store.log_event({"user_id": uid, "type": "share", "share_type": "herb_request", "ref_id": req["id"],
                         "target_platform": "line", "status": "created", "at": G.now_utc().isoformat()})
        return jsonify({"request": {"id": req["id"], "expires_at": req["expires_at"]},
                        "message": {"type": "flex", "altText": alt, "contents": bubble}})

    @bp.route("/api/liff/gm/request/<req_id>", methods=["GET"])
    def gm_request_view(req_id):
        uid, err = _auth()
        if err:
            return err
        try:
            return jsonify(service.request_view(uid, req_id))
        except G.GmError as e:
            return _gm_error(e)

    @bp.route("/api/liff/gm/request/<req_id>/fulfill", methods=["POST"])
    def gm_request_fulfill(req_id):
        uid, err = _auth()
        if err:
            return err
        try:
            return jsonify(service.request_fulfill(uid, req_id))
        except G.GmError as e:
            return _gm_error(e)

    @bp.route("/api/liff/gm/notices/seen", methods=["POST"])
    def gm_notices_seen():
        uid, err = _auth()
        if err:
            return err
        service.notices_seen(uid)
        return jsonify({"ok": True})

    # -------------------------------------------------------------- 開發開關（demo 用）
    def _dev_allowed(target_uid):
        if (os.getenv("GM_DEMO_MODE") or "").strip() != "1":
            return False, "GM_DEMO_MODE 未開啟"
        secret = (os.getenv("GM_DEV_SECRET") or os.getenv("CRON_SECRET") or "").strip()
        if not secret:
            return False, "未設定 GM_DEV_SECRET"
        given = (request.headers.get("X-GM-Dev-Secret") or request.headers.get("Authorization") or "").strip()
        if given.startswith("Bearer "):
            given = given[7:].strip()
        if given != secret:
            return False, "密鑰錯誤"
        allow = [x.strip() for x in (os.getenv("GM_DEV_USER_IDS") or "").split(",") if x.strip()]
        if allow and target_uid not in allow:
            return False, "此帳號不在 GM_DEV_USER_IDS 白名單內"
        return True, ""

    @bp.route("/api/gm/dev/<action>", methods=["POST"])
    def gm_dev(action):
        data = request.get_json(force=True, silent=True) or {}
        target = (data.get("user_id") or "").strip()
        ok, why = _dev_allowed(target)
        if not ok:
            return jsonify({"error": why}), 403
        if not target:
            return jsonify({"error": "缺少 user_id"}), 400
        try:
            return jsonify(service.dev(target, action, data.get("params") or {}))
        except G.GmError as e:
            return _gm_error(e)

    app.register_blueprint(bp)
    return GmHooks(service)
