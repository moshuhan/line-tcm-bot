# -*- coding: utf-8 -*-
"""
把考選部官方的「題目 PDF + 答案 PDF」成對檔案，直接解析成
data/exam_questions.json 用的題庫格式。不需要人工謄打——這兩份 PDF
是文字版（不是掃描圖檔），可以直接抓文字解析。

用法：
    python scripts/import_exam_pdfs.py "D:\\中醫師國考\\115(一)"            # 預覽
    python scripts/import_exam_pdfs.py "D:\\中醫師國考\\115(一)" --write    # 實際寫入

規則：
- 在指定資料夾裡找所有「...題目.pdf」，each 找同名把「題目」換成「答案」的檔案配對
- 年度／梯次／階段／科目代號／科目名稱，直接從 PDF 自己的表頭文字解析，
  不依賴資料夾命名（資料夾名稱可能只是人自己習慣的簡稱，容易跟真正的
  「第幾次」「第幾階段」搞混，PDF 內文才是官方權威來源）
- 科目名稱 → category 用 _SUBJECT_CATEGORY_MAP 對照到現有分類，
  對不到的科目會在結尾列出來，需要人工補對照規則
- concepts 欄位留空陣列，交給之後的考點分群腳本
- explanation 留 null，交給之後「即時生成＋首次快取」的機制
- 答案解析優先用 pdfplumber 的表格解析（對「題號/答案」表格版面最穩定，
  不受 pypdf 純文字擷取把表格順序打散影響），pdfplumber 沒裝或抓不到表格時
  才退回舊的逐行文字解析
- 111 年（分階段考試改制前）用不同字型，選項符號被 pypdf 讀成私有區
  （Private Use Area）字元而不是「A.」這種文字，題號也是用空格分隔而不是句點，
  這裡都有對應處理；PUA 字元一律用 chr(0x....) 在程式內組出來，不要在原始碼裡
  直接貼特殊字元（貼了容易在編輯/傳輸過程中被靜默清空成空字串）
"""
import argparse
import glob
import json
import os
import re
import sys
import unicodedata

try:
    from pypdf import PdfReader
except ImportError:
    print("需要 pypdf：pip install pypdf")
    sys.exit(1)

try:
    import pdfplumber
except ImportError:
    pdfplumber = None  # 答案解析會退回舊的純文字行解析，對表格被拆散的年份不夠穩固

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "exam_questions.json")

_FULLWIDTH_LETTERS = {"Ａ": "A", "Ｂ": "B", "Ｃ": "C", "Ｄ": "D", "Ｅ": "E", "Ｆ": "F"}

# 111 年（分階段考試改制前）PDF 用的非標準字型，選項符號被讀成私有區字元。
# 之後如果遇到新年份又映射到別的 PUA 碼位，把新的 codepoint 加進這個 list 即可。
_PUA_OPTION_CODEPOINTS = [0xE18C, 0xE18D, 0xE18E, 0xE18F]
_PUA_OPTION_MAP = {chr(cp): letter for cp, letter in zip(_PUA_OPTION_CODEPOINTS, "ABCD")}
_OPTION_MARKER_RE = re.compile(
    r"([A-F])[.\uFF0E]|(" + "|".join(chr(cp) for cp in _PUA_OPTION_CODEPOINTS) + ")"
)
# 111 年表頭裡穿插的私有區「註腳標記」字元（不帶任何資訊，純排版用），解析表頭前先拿掉，
# 避免插在「科目：中醫基礎醫學《這裡》（包括...）」中間卡住 regex。
_HEADER_NOISE_PUA_RE = re.compile(r"[\uE100-\uE17F\uE200-\uE2FF]")

# 科目名稱關鍵字 → 現有題庫的分類。之後有新科目名稱對不上，
# 會被列在警告裡，補一條規則到這裡即可，不用改解析邏輯。
_SUBJECT_CATEGORY_MAP = [
    (("中醫基礎理論", "內經", "難經", "中醫醫學史"), "基礎理論"),
    (("中藥學", "生藥學", "本草"), "中藥學"),
    (("方劑學",), "方劑學"),
    (("針灸", "經絡", "腧穴"), "針灸學"),
    (("五官科",), "五官科"),
    (("中醫外科", "中醫傷科", "外科學", "傷科學"), "外科傷科"),
    (("中醫內科", "中醫婦科", "中醫兒科", "傷寒", "溫病", "辨證論治"), "辨證論治"),
]


def _guess_category(subject_name_full):
    """
    依科目全名猜分類。只看「（包括...）」括號內的內容，不看前面的科目大標題——
    因為官方科目名稱習慣統一叫「中醫基礎醫學（一）／（二）」，大標題本身完全
    不能拿來區分實際考的是基礎理論還是方劑/中藥，真正有意義的關鍵字都在
    「包括」後面那段列出的細項科目裡。
    """
    m = re.search(r"[（(]包括(.+?)[）)]", subject_name_full)
    text_to_match = m.group(1) if m else subject_name_full
    for keywords, category in _SUBJECT_CATEGORY_MAP:
        if any(kw in text_to_match for kw in keywords):
            return category
    return None


def _nfc(s):
    """
    Unicode NFC 正規化。107 年的 PDF 把「年」寫成 U+F98E（CJK 相容字元），
    視覺上跟標準的「年」(U+5E74) 完全一樣，但 regex 認不得。NFC 只會把相容字元
    換回標準字，不會像 NFKC 那樣把全形標點（，。：）轉成半形，不會動到題目內文。
    """
    return unicodedata.normalize("NFC", s)


def _extract_full_text(path):
    reader = PdfReader(path)
    return _nfc("\n".join((p.extract_text() or "") for p in reader.pages))


def _parse_header(text):
    """
    從題目或答案 PDF 的表頭抓 年度／階段／梯次／科目代號／科目名稱。任何一項抓不到就回傳 None。
    考選部不同年份的考試名稱寫法不一致（有的用「專技高考」縮寫，有的用
    「專門職業及技術人員高等考試」全名），所以年度/梯次不依賴特定措辭字串，只依
    文件最前面固定會出現「NNN年」「第N次」「第N階段考試」這幾個獨立的小片段各自抓。
    """
    head = _HEADER_NOISE_PUA_RE.sub("", text[:800])  # 這些欄位都在文件最前面，縮小範圍避免題目內文干擾
    year_m = re.search(r"(\d{2,3})\s*年", head)
    session_m = re.search(r"第([一二三四五])次", head)
    stage_m = re.search(r"第([一二三四五])階段考試", head)
    # 科目名稱可能跨行斷字（例如「中醫證治\n學」），用 DOTALL 讓 . 吃到換行，
    # 直到遇到「（包括...）」右括號、「(試題代號」或「考試時間」其中之一才停。
    # 「科目」前面的寫法不只一種：112 年後是「科目名稱：」，111 年（改制前）
    # 是「科 目：」（中間有空格、沒有「名稱」二字），兩種都要接受。
    subject_m = re.search(
        r"科\s*目(?:名稱)?\s*[：:]\s*(.+?)(?:\s*[（(]試題代號|考試時間|\n\s*題\s*數)",
        head, re.DOTALL,
    )
    code_m = re.search(r"(?:試題)?代\s*號\s*[：:]\s*(\d+)", head)
    if not (year_m and subject_m):
        return None
    # 把捕捉到的科目名稱裡所有空白／換行都拿掉，修復斷行造成的斷詞（「中醫證治\n學」→「中醫證治學」）
    full_subject = re.sub(r"\s+", "", subject_m.group(1))
    short_subject = re.split(r"[（(]包括", full_subject)[0]

    if stage_m:
        exam_stage = f"第{stage_m.group(1)}階段"
    elif "基礎醫學" in full_subject:
        # 某些年份（例如 111 年改制前）的表頭完全沒有「第N階段考試」這句話，
        # 只能靠科目名稱本身的規律推斷：基礎醫學＝第一階段、臨床醫學＝第二階段。
        exam_stage = "第一階段"
    elif "臨床醫學" in full_subject:
        exam_stage = "第二階段"
    else:
        exam_stage = ""

    return {
        "exam_year": year_m.group(1),
        "exam_session": f"第{session_m.group(1)}次" if session_m else "",
        "exam_stage": exam_stage,
        "subject_code": code_m.group(1) if code_m else "",
        "subject_name": short_subject,       # 顯示用短名稱，例如「中醫基礎醫學（二）」
        "subject_name_full": full_subject,   # 含「（包括...）」的完整科目描述，只用來判斷分類
    }


def _parse_questions(text):
    """
    回傳 {題號: {"stem":..., "options": {...}}}，解析失敗的題號不會出現在結果裡。
    題號分隔符號支援兩種寫法：「1.」（112 年後，句點）跟「1 」（111 年改制前，純空格）。
    選項符號支援「A.」文字或私有區字元（111 年改制前的字型），且不假設每個選項
    各自獨立一行——短答案選項可能擠在同一行用空格分隔，用「找下一個選項符號的
    位置」當作邊界，不依賴換行。
    """
    # \u300C\u7121\u53E5\u9EDE\u300D\u5BEB\u6CD5\uFF08111 \u5E74\u524D\uFF09\u7684\u5206\u9694\u53EA\u80FD\u662F\u540C\u4E00\u884C\u7684\u7A7A\u683C/tab\uFF0C\u800C\u4E14\u5F8C\u9762\u5FC5\u9808\u7DCA\u63A5\u5167\u5BB9\u2014\u2014
    # \u4E0D\u80FD\u7528 \s+\uFF0C\u5426\u5247\u984C\u76EE\u9644\u5716\u7684\u7368\u7ACB\u6A19\u865F\uFF08\u4F8B\u5982\u5716\u4E0A\u7684\u300C3\u300D\u55AE\u7368\u4E00\u884C\uFF09\u6703\u628A\u5F8C\u9762\u7684\u63DB\u884C
    # \u4E00\u8D77\u541E\u6389\uFF0C\u5C0E\u81F4\u7DCA\u63A5\u8457\u7684\u4E0B\u4E00\u984C\u984C\u865F\u4E0D\u518D\u4F4D\u65BC\u884C\u9996\u3001\u6574\u6BB5\u5F8C\u7E8C\u984C\u76EE\u88AB\u8AA4\u4F75\u56DE\u4E0A\u4E00\u984C\u3002
    first_q = re.search(r"(?:^|\n)\s*1(?:[.\uFF0E]\s*|[ \t]+(?=\S))\S", text)
    body = text[first_q.start():] if first_q else text
    parts = re.split(r"\n[ \t]*(\d{1,3})(?:[.\uFF0E]\s*|[ \t]+(?=\S))", "\n" + body)
    raw_pairs = list(zip(parts[1::2], parts[2::2]))

    # 有些臨床題會夾帶檢驗數值參考範圍（例如「0.8-1.9 ng/dL」），PDF 斷行後可能
    # 剛好變成「換行＋數字＋句點/空格」，被誤判成新題號，導致原本那一題的選項被切走。
    # 用「題號必須接續上一題 +1」過濾掉這種假陽性——真正的題號一定連續，誤判出來
    # 的數字幾乎不可能剛好接得上，接不上的直接併回上一題的內容。
    pairs = []
    expected_next = 1
    for num, content in raw_pairs:
        if pairs and int(num) != expected_next:
            pairs[-1] = (pairs[-1][0], pairs[-1][1] + num + "." + content)
            continue
        pairs.append((num, content))
        expected_next = int(num) + 1

    questions = {}
    for num, content in pairs:
        matches = list(_OPTION_MARKER_RE.finditer(content))
        if len(matches) < 2:
            continue
        # 短選項（例如純數字）的版面是「內容在標記前面」（2⟨A⟩ 3⟨B⟩ 4⟨C⟩ 5⟨D⟩），
        # 特徵是最後一個標記後面沒有任何內容，這時改成用「標記前面的文字」當選項。
        if not content[matches[-1].end():].strip():
            line_start = content.rfind("\n", 0, matches[0].start()) + 1
            stem = content[:line_start].strip().replace("\n", "")
            options = {}
            prev_end = line_start
            for mo in matches:
                letter = mo.group(1) or _PUA_OPTION_MAP.get(mo.group(2))
                opt_text = content[prev_end:mo.start()].strip().replace("\n", "")
                if letter and opt_text:
                    options[letter] = opt_text
                prev_end = mo.end()
            if len(options) >= 2:
                questions[num] = {"stem": stem, "options": options}
            continue
        stem = content[:matches[0].start()].strip().replace("\n", "")
        options = {}
        for i, mo in enumerate(matches):
            letter = mo.group(1) or _PUA_OPTION_MAP.get(mo.group(2))
            start = mo.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            opt_raw = content[start:end]
            if i + 1 == len(matches):
                # 題目附圖上的獨立標號（例如圖中的 1、2、3）會出現在最後一個選項後面，
                # 各自單獨一行，拿掉行尾這些「只有數字的行」避免污染選項內容。
                lines = opt_raw.split("\n")
                while len(lines) > 1 and re.fullmatch(r"\s*\d{1,3}\s*", lines[-1]):
                    lines.pop()
                opt_raw = "\n".join(lines)
            opt_text = opt_raw.strip().replace("\n", "")
            if letter and opt_text:
                options[letter] = opt_text
        if len(options) >= 2:
            questions[num] = {"stem": stem, "options": options}
    return questions


def _parse_answers_table(pdf_path):
    """
    優先解法：用 pdfplumber 的表格解析取「題號/答案」表。對兩種年代格式都適用
    （112 年後的「題序」多欄小表，跟 111 年改制前的「第N題」寬版大表），比純文字
    逐行解析穩固很多——pypdf 的純文字擷取對複雜表格常常把視覺順序打散。
    回傳 {題號(int): 答案字母}；「＃」（爭議題多重給分）或空白儲存格不會出現在結果裡，
    呼叫端看到題號對不到答案，會把該題跳過並發出警告，不會隨便挑一個字母當正確答案。
    """
    if pdfplumber is None:
        return {}
    answers = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                header_row = None
                for row in table:
                    label = _nfc(row[0] or "").strip() if row else ""
                    if "題" in label and ("號" in label or "序" in label):
                        header_row = row
                        continue
                    if header_row and label.startswith("答案"):
                        for qcell, acell in zip(header_row[1:], row[1:]):
                            if not qcell or not acell:
                                continue
                            qnum_digits = re.sub(r"\D", "", _nfc(str(qcell)))
                            if not qnum_digits:
                                continue
                            letter = _FULLWIDTH_LETTERS.get(str(acell).strip(), str(acell).strip())
                            if letter in ("A", "B", "C", "D", "E", "F"):
                                answers[int(qnum_digits)] = letter
                        header_row = None
    return answers


def _parse_answers_lines(text):
    """
    備用解法（pdfplumber 沒裝，或抓不到表格時才用）：逐行找「答案」開頭的行，
    依出現順序當作題號 1, 2, 3...。比表格解析脆弱（純文字順序可能被打散），
    只在表格解析失敗時當 fallback。
    """
    answers = {}
    idx = 0
    for line in text.splitlines():
        if line.strip().startswith("答案"):
            tokens = re.findall(r"[ＡＢＣＤＥＦABCDEF＃#]", line.split("答案", 1)[1])
            for t in tokens:
                idx += 1
                if t in ("＃", "#"):
                    continue
                answers[idx] = _FULLWIDTH_LETTERS.get(t, t)
    return answers


# 題幹依賴附圖才能作答的常見寫法。刻意不用單純的「圖」字——「河圖」「針灸圖經」
# 這類書名/名詞會誤判。PDF 文字擷取抓不到圖片，這類題目放進刷題系統會是壞題，
# 所以標記起來，之後由使用者決定要排除還是補圖。
_IMAGE_REF_RE = re.compile(r"以圖|如圖|下圖|上圖|右圖|左圖|圖中|圖示|附圖|何圖|依圖|見圖|圖為|觀察圖|下列圖")

_TOTAL_QUESTIONS_RE = re.compile(r"(?:單選)?題\s*數[：:]\s*(\d+)\s*題|共\s*(\d+)\s*題")


def _extract_total_questions(text):
    """
    從表頭抓「本科目共80題」或「題　　數：80題」這種官方公告的實際題數，
    當作驗證用的權威基準——不能用「解析出幾題」自己當基準，那樣任何一題
    解析失敗（例如下面這種選項是圖片、抓不到文字的題目）都會被基準數字
    自動吸收掉，完全看不出來少了題目。抓不到就回傳 None，由呼叫端 fallback。
    """
    m = _TOTAL_QUESTIONS_RE.search(text[:1500])
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def parse_pair(question_pdf_path, answer_pdf_path):
    """
    解析一組題目/答案 PDF，回傳 (questions_list, warnings_list)。
    questions_list 裡每一項已經是最終要寫進 exam_questions.json 的格式。
    """
    warnings = []
    q_text = _extract_full_text(question_pdf_path)
    header = _parse_header(q_text)
    if not header:
        warnings.append(f"{os.path.basename(question_pdf_path)}：無法解析表頭（年度／科目名稱），已略過整份檔案")
        return [], warnings

    category = _guess_category(header["subject_name_full"])
    if not category:
        warnings.append(f"科目「{header['subject_name']}」對不到現有分類，這批題目的 category 會是 null，需要人工指定")

    parsed_questions = _parse_questions(q_text)

    answers = {}
    a_text = ""
    if os.path.isfile(answer_pdf_path):
        a_text = _extract_full_text(answer_pdf_path)
        answers = _parse_answers_table(answer_pdf_path)
        if not answers:
            answers = _parse_answers_lines(a_text)
    else:
        warnings.append(f"找不到對應答案檔：{answer_pdf_path}，這批題目會沒有 answer")

    # 官方公告的真實題數，優先看答案卷（兩種年代格式都有寫），題目卷抓不到就 fallback
    # 到「已成功解析出幾題」——這只是保底，不是理想基準，只在真的抓不到官方數字時才用。
    total_questions = (
        _extract_total_questions(a_text)
        or _extract_total_questions(q_text)
        or len(parsed_questions)
    )

    results = []
    for i in range(1, total_questions + 1):
        num_str = str(i)
        qdata = parsed_questions.get(num_str)
        if not qdata:
            warnings.append(
                f"第 {num_str} 題（{header['subject_name']}）沒有解析出文字內容，已略過"
                "（常見原因：選項是圖片而非文字，PDF 文字擷取抓不到圖片內容）"
            )
            continue
        answer = answers.get(i, "")
        if not answer:
            warnings.append(f"第 {num_str} 題（{header['subject_name']}）沒有對到答案，已略過")
            continue
        qid = f"{header['exam_year']}-{header['subject_code'] or 'X'}-{num_str.zfill(3)}"
        results.append({
            "id": qid,
            "exam_year": header["exam_year"],
            "exam_session": header["exam_session"],
            "exam_stage": header["exam_stage"],
            "subject_code": header["subject_code"],
            "subject_name": header["subject_name"],
            "category": category,
            "question_type": "單選題",
            "question": qdata["stem"],
            "options": qdata["options"],
            "answer": answer,
            "explanation": None,
            "concepts": [],
            "requires_image": bool(_IMAGE_REF_RE.search(qdata["stem"])),
        })
        if results[-1]["requires_image"]:
            warnings.append(f"第 {num_str} 題（{header['subject_name']}）題幹提到附圖，沒有圖無法作答，已標記 requires_image=true")

    if len(results) != total_questions:
        warnings.append(f"{header['subject_name']}：官方公告共 {total_questions} 題，實際成功組出 {len(results)} 題")

    return results, warnings


def find_pairs(folder):
    """在資料夾裡找所有「...題目.pdf」並配對同目錄下把「題目」換成「答案」的檔案。"""
    pairs = []
    for q_path in glob.glob(os.path.join(folder, "*題目.pdf")):
        a_path = q_path.replace("題目.pdf", "答案.pdf")
        pairs.append((q_path, a_path))
    return pairs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", help="放題目/答案 PDF 的資料夾路徑")
    parser.add_argument("--write", action="store_true", help="實際寫入/合併進 data/exam_questions.json（不加只會預覽）")
    args = parser.parse_args()

    pairs = find_pairs(args.folder)
    if not pairs:
        print(f"在 {args.folder} 找不到任何「...題目.pdf」檔案")
        sys.exit(1)

    all_questions = []
    all_warnings = []
    for q_path, a_path in pairs:
        qs, warns = parse_pair(q_path, a_path)
        print(f"{os.path.basename(q_path)}: 解析出 {len(qs)} 題")
        all_questions.extend(qs)
        all_warnings.extend(warns)

    print(f"\n總計 {len(all_questions)} 題")
    if all_warnings:
        print("\n=== 警告 ===")
        for w in all_warnings:
            print(f"[WARN] {w}")

    if all_questions:
        print("\n範例第一題：")
        print(json.dumps(all_questions[0], ensure_ascii=False, indent=2))

    if args.write:
        existing = []
        if os.path.isfile(OUTPUT_PATH):
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        existing_ids = {q.get("id") for q in existing}
        new_ones = [q for q in all_questions if q["id"] not in existing_ids]
        merged = existing + new_ones
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        print(f"\n已合併寫入 {OUTPUT_PATH}（新增 {len(new_ones)} 題，跳過 {len(all_questions) - len(new_ones)} 題重複 id，原本已有 {len(existing)} 題）")
    else:
        print("\n（這是預覽模式，沒有寫入檔案。確認沒問題後加 --write 參數正式寫入。）")


if __name__ == "__main__":
    main()
