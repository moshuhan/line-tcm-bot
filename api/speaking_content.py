# -*- coding: utf-8 -*-
"""
口說 LIFF 的內容層：Case Generator（臨床衛教）＋ Topic Generator（學術討論）。

對應規格書第六～八節：「情境不是固定腳本，而是可以持續新增、更新與擴充」。
Case／Topic 內容存於 data/clinical_cases.json、data/academic_topics.json，
做法比照 exam_quiz.py／tcm_master_knowledge.json 的既有模式——JSON 檔案
載入記憶體快取，之後只讀不寫。新增病例／題目只需要編輯 JSON 檔案重新
部署，不需要改這個模組或 Patient/Professor Agent 的程式碼。
"""
import os
import json
import random

_CASES_CACHE = None
_TOPICS_CACHE = None


def _data_path(filename):
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", filename))


def _load_json_list(path):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_cases():
    """載入所有臨床病例，記憶體快取。"""
    global _CASES_CACHE
    if _CASES_CACHE is None:
        _CASES_CACHE = _load_json_list(_data_path("clinical_cases.json"))
    return _CASES_CACHE


def load_topics():
    """載入所有學術討論題目，記憶體快取。"""
    global _TOPICS_CACHE
    if _TOPICS_CACHE is None:
        _TOPICS_CACHE = _load_json_list(_data_path("academic_topics.json"))
    return _TOPICS_CACHE


def get_case_by_id(case_id):
    for c in load_cases():
        if c.get("case_id") == case_id:
            return c
    return None


def get_topic_by_id(topic_id):
    for t in load_topics():
        if t.get("topic_id") == topic_id:
            return t
    return None


def get_random_case(difficulty=None):
    """
    隨機抽一個病例（Case Generator）。difficulty 可篩選 beginner/intermediate/advanced，
    該難度沒有病例時 fallback 到全部病例隨機抽，避免因為內容庫還小而直接回傳 None。
    """
    cases = load_cases()
    if not cases:
        return None
    pool = [c for c in cases if not difficulty or c.get("difficulty") == difficulty]
    if not pool:
        pool = cases
    return random.choice(pool)


def get_random_topic(difficulty=None):
    """隨機抽一個學術討論題目（Topic Generator）。difficulty 篩選邏輯同 get_random_case。"""
    topics = load_topics()
    if not topics:
        return None
    pool = [t for t in topics if not difficulty or t.get("difficulty") == difficulty]
    if not pool:
        pool = topics
    return random.choice(pool)
