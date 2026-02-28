#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本地測試入口。執行與 api/index.py 相同的 Flask 應用。
Vercel 部署使用 api/index.py，本檔僅供開發時用 python test_local.py 啟動。
"""
if __name__ == "__main__":
    from api.index import app
    app.run(host="0.0.0.0", port=5000, debug=True)
