# -*- coding: utf-8 -*-
"""
遊戲化使用者資料存取。

每位使用者一份文件（collection: gm_users），內含 XP／連續天數／圖鑑／方劑／茶飲／關卡進度／
每日計數／已結算紀錄。單文件加樂觀鎖（_v）：讀 → 改 → 以 _v 條件寫回，衝突就重試，
避免同一個人同時送出兩個請求（例如網路重送）造成重複發獎。

正式環境用 MongoDB（跟其他功能共用 index.py 的 mongo_db）。本機連不到 Railway 內網時，
自動退回「記憶體＋JSON 檔」的備援儲存，讓整套流程可以在本機直接測試（不會碰到正式資料）。
"""
import json
import os
import tempfile
import threading

COLL_USERS = "gm_users"
COLL_EVENTS = "gm_events"
COLL_REQUESTS = "gm_requests"

MAX_RETRY = 6


class Conflict(Exception):
    pass


class Store:
    def __init__(self, db=None, local_path=None):
        self.db = db
        self.local_path = local_path or os.getenv("GM_LOCAL_STORE") or os.path.join(
            tempfile.gettempdir(), "gm_local_store.json")
        self._lock = threading.RLock()
        self._mem = None  # 備援儲存：{"users": {uid: doc}, "events": [..]}
        if self.db is None:
            self._load_local()

    # ---------------------------------------------------------- 備援儲存
    def _load_local(self):
        try:
            with open(self.local_path, "r", encoding="utf-8") as f:
                self._mem = json.load(f)
        except Exception:
            self._mem = {"users": {}, "events": [], "requests": {}}

    def _flush_local(self):
        try:
            with open(self.local_path, "w", encoding="utf-8") as f:
                json.dump(self._mem, f, ensure_ascii=False)
        except Exception:
            pass

    @property
    def is_mongo(self):
        return self.db is not None

    # ---------------------------------------------------------- 讀寫
    def load(self, user_id):
        if self.db is not None:
            doc = self.db[COLL_USERS].find_one({"user_id": user_id})
            if doc:
                doc.pop("_id", None)
            return doc
        with self._lock:
            doc = self._mem["users"].get(user_id)
            return json.loads(json.dumps(doc)) if doc else None

    def _save(self, doc):
        """樂觀鎖寫回；版本不符丟 Conflict。"""
        uid = doc["user_id"]
        old = int(doc.get("_v", 0))
        doc["_v"] = old + 1
        if self.db is not None:
            col = self.db[COLL_USERS]
            if old == 0:
                try:
                    col.insert_one(dict(doc))
                except Exception as e:
                    # 重複主鍵＝別的請求剛建立了這位使用者
                    raise Conflict() from e
            else:
                res = col.replace_one({"user_id": uid, "_v": old}, dict(doc))
                if res.matched_count != 1:
                    raise Conflict()
            return
        with self._lock:
            cur = self._mem["users"].get(uid)
            cur_v = int(cur.get("_v", 0)) if cur else 0
            if cur_v != old:
                raise Conflict()
            self._mem["users"][uid] = json.loads(json.dumps(doc))
            self._flush_local()

    def ensure_index(self):
        if self.db is not None:
            try:
                self.db[COLL_USERS].create_index("user_id", unique=True)
            except Exception:
                pass

    def update(self, user_id, fn, new_doc_factory):
        """
        讀取（沒有就建立）→ 呼叫 fn(doc) 修改 → 寫回。回傳 fn 的回傳值。
        fn 如果丟例外，不會寫回。遇到版本衝突自動重試。
        """
        for _ in range(MAX_RETRY):
            doc = self.load(user_id) or new_doc_factory(user_id)
            result = fn(doc)
            try:
                self._save(doc)
                return result
            except Conflict:
                continue
        raise Conflict("too many concurrent updates")

    def read(self, user_id, new_doc_factory):
        """唯讀取得（沒有就回傳一份尚未存檔的新文件）。"""
        return self.load(user_id) or new_doc_factory(user_id)

    # ---------------------------------------------------------- 事件紀錄（只新增）
    def log_event(self, event):
        try:
            if self.db is not None:
                self.db[COLL_EVENTS].insert_one(dict(event))
            else:
                with self._lock:
                    self._mem["events"].append(event)
                    self._mem["events"] = self._mem["events"][-500:]
                    self._flush_local()
        except Exception:
            pass

    # ---------------------------------------------------------- 求藥卡（HerbRequest）
    def create_request(self, req):
        if self.db is not None:
            self.db[COLL_REQUESTS].insert_one(dict(req))
            return
        with self._lock:
            self._mem.setdefault("requests", {})[req["id"]] = dict(req)
            self._flush_local()

    def get_request(self, req_id):
        if self.db is not None:
            d = self.db[COLL_REQUESTS].find_one({"id": req_id})
            if d:
                d.pop("_id", None)
            return d
        with self._lock:
            d = self._mem.setdefault("requests", {}).get(req_id)
            return dict(d) if d else None

    def list_requests(self, requester, status=None):
        q = {"requester": requester}
        if status:
            q["status"] = status
        if self.db is not None:
            return [{k: v for k, v in d.items() if k != "_id"} for d in self.db[COLL_REQUESTS].find(q).limit(100)]
        with self._lock:
            return [dict(d) for d in self._mem.setdefault("requests", {}).values()
                    if d.get("requester") == requester and (not status or d.get("status") == status)]

    def claim_request(self, req_id, fields):
        """原子地把 open 的求藥卡改成 fulfilled（只有一個人能成功）。成功回傳更新後的文件，失敗回傳 None。"""
        if self.db is not None:
            from pymongo import ReturnDocument
            d = self.db[COLL_REQUESTS].find_one_and_update(
                {"id": req_id, "status": "open"}, {"$set": dict(fields, status="fulfilled")},
                return_document=ReturnDocument.AFTER)
            if d:
                d.pop("_id", None)
            return d
        with self._lock:
            d = self._mem.setdefault("requests", {}).get(req_id)
            if not d or d.get("status") != "open":
                return None
            d.update(fields)
            d["status"] = "fulfilled"
            self._flush_local()
            return dict(d)

    def update_request(self, req_id, fields):
        if self.db is not None:
            self.db[COLL_REQUESTS].update_one({"id": req_id}, {"$set": dict(fields)})
            return
        with self._lock:
            d = self._mem.setdefault("requests", {}).get(req_id)
            if d:
                d.update(fields)
                self._flush_local()

    def list_users(self, limit=50):
        if self.db is not None:
            return [{k: v for k, v in d.items() if k != "_id"} for d in self.db[COLL_USERS].find({}).limit(limit)]
        with self._lock:
            return list(self._mem["users"].values())[:limit]

    def reset_user(self, user_id):
        if self.db is not None:
            self.db[COLL_USERS].delete_one({"user_id": user_id})
            return
        with self._lock:
            self._mem["users"].pop(user_id, None)
            self._flush_local()
