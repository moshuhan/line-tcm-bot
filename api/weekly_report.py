# -*- coding: utf-8 -*-
"""
每週學習分析報告：從 Redis 彙整提問、NLP 概念聚類、產出 PDF 並寄送。
"""

import io
import json
import os
import smtplib
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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
    """彙整提問並回傳前 N 大困惑觀念 [(concept, count), ...]。"""
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
    for c in all_concepts:
        c = (c or "其他").strip() or "其他"
        counts[c] = counts.get(c, 0) + 1
    sorted_concepts = sorted(counts.items(), key=lambda x: -x[1])
    return sorted_concepts[:top_n]


def _draw_chart_bytes(concept_counts):
    """用 matplotlib 繪製提問次數長條圖，回傳 PNG bytes。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["SimHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
        concepts = [c[0] for c in concept_counts]
        counts = [c[1] for c in concept_counts]
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(range(len(concepts)), counts, color="steelblue", edgecolor="navy")
        ax.set_xticks(range(len(concepts)))
        ax.set_xticklabels(concepts, rotation=45, ha="right")
        ax.set_ylabel("提問次數")
        ax.set_title("本週前十大困惑觀念（提問次數）")
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close()
        buf.seek(0)
        return buf.read()
    except Exception:
        return None


def build_pdf(concept_counts, chart_bytes=None):
    """使用 ReportLab 產出 PDF，可嵌入圖表。"""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        return None
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    story = []
    story.append(Paragraph("每週學習分析報告", styles["Title"]))
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph("前十大困惑觀念（依提問次數）", styles["Heading2"]))
    story.append(Spacer(1, 0.3*cm))
    data = [["排名", "概念", "提問次數"]]
    for i, (c, n) in enumerate(concept_counts, 1):
        data.append([str(i), c, str(n)])
    t = Table(data, colWidths=[2*cm, 6*cm, 3*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), "lightgrey"),
        ("GRID", (0, 0), (-1, -1), 0.5, "grey"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
    ]))
    story.append(t)
    if chart_bytes:
        story.append(Spacer(1, 0.5*cm))
        try:
            img = Image(io.BytesIO(chart_bytes), width=14*cm, height=7*cm)
            story.append(img)
        except Exception:
            pass
    doc.build(story)
    buf.seek(0)
    return buf.read()


def send_report_email(pdf_bytes, to_email, smtp_config):
    """透過 SMTP 寄送 PDF 報告。"""
    if not pdf_bytes or not to_email:
        return False
    host = smtp_config.get("host") or os.getenv("SMTP_HOST")
    port = int(smtp_config.get("port") or os.getenv("SMTP_PORT") or 587)
    user = smtp_config.get("user") or os.getenv("SMTP_USER")
    password = smtp_config.get("password") or os.getenv("SMTP_PASSWORD")
    if not host or not user or not password:
        return False
    try:
        msg = MIMEMultipart()
        msg["Subject"] = "LINE TCM Bot 每週學習分析報告"
        msg["From"] = user
        msg["To"] = to_email
        msg.attach(MIMEText("本週前十大困惑觀念報告如附件。", "plain", "utf-8"))
        att = MIMEApplication(pdf_bytes, _subtype="pdf")
        att.add_header("Content-Disposition", "attachment", filename="weekly_learning_report.pdf")
        msg.attach(att)
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        return True
    except Exception:
        return False


def run_weekly_report(redis_client, openai_client, report_email=None, smtp_config=None):
    """執行每週報告：彙整、前十大概念、圖表、PDF、寄信。回傳 (success: bool, message: str)。"""
    report_email = report_email or os.getenv("REPORT_EMAIL")
    if not report_email:
        return False, "REPORT_EMAIL 未設定"
    top = get_top_confused_concepts(redis_client, openai_client, top_n=TOP_N_CONCEPTS)
    if not top:
        return True, "本週無提問資料，未產出報告"
    chart_bytes = _draw_chart_bytes(top)
    pdf_bytes = build_pdf(top, chart_bytes)
    if not pdf_bytes:
        return False, "PDF 產出失敗"
    smtp_config = smtp_config or {}
    if send_report_email(pdf_bytes, report_email, smtp_config):
        return True, "報告已寄送至 " + report_email
    return False, "寄送失敗，請檢查 SMTP 與 REPORT_EMAIL"
