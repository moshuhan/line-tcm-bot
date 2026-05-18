# -*- coding: utf-8 -*-
"""
每週學習分析報告：從 Redis 彙整提問、NLP 概念聚類、產出 PDF 並寄送。
"""

import base64
import glob
import io
import json
import os
import time
import warnings

from openai import OpenAI

# 前十大困惑觀念
TOP_N_CONCEPTS = 10
BATCH_SIZE = 20


def _fetch_questions(redis_client):
    """從 Redis 取出本週提問（最近 QUESTION_LOG_MAX 筆，依 ts 篩選最近 7 天）。"""
    if not redis_client:
        return []
    try:
        raw = redis_client.lrange("question_log", 0, -1)
        if not raw:
            return []
        out = []
        now = time.time()
        week_ago = now - 7 * 24 * 3600
        for r in raw:
            try:
                s = r.decode("utf-8") if hasattr(r, "decode") else str(r)
                obj = json.loads(s)
                ts = obj.get("ts", 0)
                if ts >= week_ago and obj.get("text"):
                    out.append(obj)
            except Exception:
                pass
        return out
    except Exception:
        return []


def _assign_concepts_batch(openai_client, texts):
    """用 GPT 為一批問題各指派一個「概念」標籤（中文，簡短）。"""
    if not texts:
        return []
    try:
        prompt = "以下為學生提問，請為「每一行」依序回傳一個簡短中文概念（如：經絡、穴位、辨證、氣、陰陽五行、中藥、針灸、其他），一行一個，不要編號與多餘說明。\n\n" + "\n".join(texts[:BATCH_SIZE])
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
        )
        content = (resp.choices[0].message.content or "").strip()
        concepts = [line.strip().split()[-1] if line.strip() else "其他" for line in content.split("\n") if line.strip()]
        return concepts[:len(texts)]
    except Exception:
        return ["其他"] * min(len(texts), BATCH_SIZE)


def get_top_confused_concepts(redis_client, openai_client, top_n=TOP_N_CONCEPTS):
    """
    彙整提問並回傳前 N 大困惑觀念。
    回傳：[(concept, count, [question_texts]), ...]
    """
    questions = _fetch_questions(redis_client)
    if not questions:
        return []
    texts = [q.get("text", "") for q in questions]
    all_concepts = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        concepts = _assign_concepts_batch(openai_client, batch)
        all_concepts.extend(concepts[: len(batch)])
    while len(all_concepts) < len(texts):
        all_concepts.append("其他")
    counts = {}
    category_questions: dict = {}
    for text, concept in zip(texts, all_concepts):
        c = (concept or "其他").strip() or "其他"
        counts[c] = counts.get(c, 0) + 1
        category_questions.setdefault(c, []).append(text)
    sorted_concepts = sorted(counts.items(), key=lambda x: -x[1])
    return [(c, n, category_questions.get(c, [])) for c, n in sorted_concepts[:top_n]]


def _summarize_category_questions(openai_client, category, questions, max_q=20):
    """
    用 GPT 將某分類的學生問題整理成 3-5 個困惑重點。
    回傳 list[str]；失敗回傳空 list。
    """
    if not questions:
        return []
    q_text = "\n".join(f"- {q}" for q in questions[:max_q])
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"以下是學生在「{category}」主題下提出的問題，"
                    f"請整理出 3-5 個主要困惑重點（每點一行，簡短精準，不需要編號或符號）：\n\n"
                    f"{q_text}"
                ),
            }],
            max_tokens=300,
            temperature=0.3,
        )
        content = (resp.choices[0].message.content or "").strip()
        points = [
            line.strip().lstrip("•-·●▪·1234567890. ").strip()
            for line in content.split("\n") if line.strip()
        ]
        return [p for p in points if p][:5]
    except Exception:
        return []


def _find_cjk_font():
    """找系統上可用的 CJK 字型路徑（Railway 安裝 fonts-noto-cjk 後）。"""
    patterns = [
        "/usr/share/fonts/**/*CJKtc*Regular*",
        "/usr/share/fonts/**/*CJKsc*Regular*",
        "/usr/share/fonts/**/*CJK*Regular*",
        "/usr/share/fonts/**/*Noto*CJK*",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]
    return None


def _draw_chart_bytes(concept_counts):
    """
    繪製長條圖，x 軸用排名數字（避開中文字型問題），圖例另列概念名。
    回傳 PNG bytes；失敗回傳 None。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")
        plt.rcParams["axes.unicode_minus"] = False

        ranks  = [str(i) for i in range(1, len(concept_counts) + 1)]
        counts = [c[1] for c in concept_counts]

        fig, ax = plt.subplots(figsize=(8, 4))
        bars = ax.bar(ranks, counts, color="steelblue", edgecolor="navy")
        ax.set_xlabel("Rank")
        ax.set_ylabel("Question Count")
        ax.set_title("Top Confused Concepts This Week")
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close()
        buf.seek(0)
        return buf.read()
    except Exception:
        return None


def build_pdf(concept_counts, chart_bytes=None, category_summaries=None):
    """
    產出 PDF：
    第一頁：困惑觀念排名表 + 長條圖
    第二頁：各分類學生困惑重點條列（category_summaries 有資料時才加）
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    except ImportError:
        return None

    CJK_FONT = "STSong-Light"
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(CJK_FONT))
    except Exception:
        CJK_FONT = "Helvetica"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=2*cm, leftMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="CJKTitle",   parent=styles["Title"],
                              fontName=CJK_FONT, fontSize=16, leading=24))
    styles.add(ParagraphStyle(name="CJKHeading", parent=styles["Heading2"],
                              fontName=CJK_FONT, fontSize=12, leading=18))
    styles.add(ParagraphStyle(name="CJKBody",    parent=styles["Normal"],
                              fontName=CJK_FONT, fontSize=10, leading=16))
    styles.add(ParagraphStyle(name="CJKBullet",  parent=styles["Normal"],
                              fontName=CJK_FONT, fontSize=10, leading=16,
                              leftIndent=12, firstLineIndent=0))

    story = []

    # ── 第一頁：排名表 + 圖表 ──
    story.append(Paragraph("每週學習分析報告", styles["CJKTitle"]))
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph("前十大困惑觀念（依提問次數）", styles["CJKHeading"]))
    story.append(Spacer(1, 0.3*cm))

    data = [["排名", "概念", "提問次數"]]
    for i, (c, n) in enumerate(concept_counts, 1):
        data.append([str(i), c, str(n)])
    t = Table(data, colWidths=[2*cm, 8*cm, 3*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), "lightgrey"),
        ("GRID",       (0, 0), (-1, -1), 0.5, "grey"),
        ("FONTSIZE",   (0, 0), (-1, -1), 10),
        ("FONTNAME",   (0, 0), (-1, -1), CJK_FONT),
        ("LEADING",    (0, 0), (-1, -1), 16),
    ]))
    story.append(t)

    if chart_bytes:
        story.append(Spacer(1, 0.5*cm))
        try:
            story.append(Image(io.BytesIO(chart_bytes), width=14*cm, height=7*cm))
        except Exception:
            pass

    # ── 第二頁：各分類困惑重點 ──
    if category_summaries:
        story.append(PageBreak())
        story.append(Paragraph("各主題學生困惑重點整理", styles["CJKTitle"]))
        story.append(Spacer(1, 0.5*cm))

        for rank, (category, points) in enumerate(category_summaries.items(), 1):
            story.append(Paragraph(f"{rank}. {category}", styles["CJKHeading"]))
            if points:
                for point in points:
                    story.append(Paragraph(f"• {point}", styles["CJKBullet"]))
            else:
                story.append(Paragraph("（本週問題數量不足以整理重點）", styles["CJKBody"]))
            story.append(Spacer(1, 0.4*cm))

    doc.build(story)
    buf.seek(0)
    return buf.read()


def send_report_email(pdf_bytes, to_email, smtp_config=None):
    """透過 Resend API 寄送 PDF 報告。"""
    if not pdf_bytes or not to_email:
        return False
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        return "RESEND_API_KEY 未設定"
    try:
        import resend
        resend.api_key = api_key
        resend.Emails.send({
            "from": "TCM Bot <onboarding@resend.dev>",
            "to": [to_email],
            "subject": "LINE TCM Bot 每週學習分析報告",
            "text": "本週前十大困惑觀念報告如附件，請查收。",
            "attachments": [{
                "filename": "weekly_learning_report.pdf",
                "content": list(pdf_bytes),
            }],
        })
        return True
    except Exception as e:
        print(f"[RESEND ERROR] to={to_email} err={e}")
        return str(e)


def run_weekly_report(redis_client, openai_client, report_email=None, smtp_config=None):
    """執行每週報告：彙整、前十大概念、圖表、PDF（含第二頁困惑重點）、寄信。"""
    report_email = report_email or os.getenv("REPORT_EMAIL")
    if not report_email:
        return False, "REPORT_EMAIL 未設定"

    top_with_questions = get_top_confused_concepts(redis_client, openai_client, top_n=TOP_N_CONCEPTS)
    if not top_with_questions:
        return True, "本週無提問資料，未產出報告"

    # 第一頁用的排名資料（不含問題 list）
    concept_counts = [(c, n) for c, n, _ in top_with_questions]

    # 第二頁：為每個分類產生困惑重點摘要
    category_summaries = {}
    for category, _, questions in top_with_questions:
        points = _summarize_category_questions(openai_client, category, questions)
        category_summaries[category] = points

    chart_bytes = _draw_chart_bytes(concept_counts)
    pdf_bytes = build_pdf(concept_counts, chart_bytes, category_summaries=category_summaries)
    if not pdf_bytes:
        return False, "PDF 產出失敗"

    result = send_report_email(pdf_bytes, report_email)
    if result is True:
        return True, "報告已寄送至 " + report_email
    return False, f"寄送失敗：{result}"
