"""시뮬레이션 Markdown 보고서를 PDF로 변환한다.

Windows 기본 맑은 고딕(malgun.ttf) 등을 찾아 등록하고, reportlab으로
표·제목·본문을 페이지에 흘려 넣는다. (Plotly 차트는 포함하지 않음)
"""

from __future__ import annotations

import os
import re
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_FONT_REGISTERED = False
_FONT_NAME = "GunsanReportKR"


def _find_korean_ttf() -> Path | None:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    candidates = [
        Path(windir) / "Fonts" / "malgun.ttf",
        Path(windir) / "Fonts" / "malgunbd.ttf",
        Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _ensure_korean_font() -> str | None:
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return _FONT_NAME
    path = _find_korean_ttf()
    if path is None:
        return None
    try:
        pdfmetrics.registerFont(TTFont(_FONT_NAME, str(path)))
    except Exception:
        return None
    _FONT_REGISTERED = True
    return _FONT_NAME


def _md_inline_to_reportlab_xml(text: str) -> str:
    """마크다운 이중 별표 굵게 구간이 있으면 `<b>`…`</b>`로, 나머지는 XML 이스케이프."""
    parts = re.split(r"(\*\*.+?\*\*)", text)
    out: list[str] = []
    for p in parts:
        if len(p) >= 4 and p.startswith("**") and p.endswith("**"):
            inner = escape(p[2:-2])
            out.append(f"<b>{inner}</b>")
        else:
            out.append(escape(p))
    return "".join(out)


def _split_md_table_row(line: str) -> list[str]:
    raw = line.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [c.strip() for c in raw.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    if not cells:
        return False

    def cell_is_sep(c: str) -> bool:
        t = c.strip().replace(" ", "")
        return bool(t) and all(ch in "-:" for ch in t)

    return all(cell_is_sep(c) for c in cells if c.strip()) and any(cells)


def _parse_md_table(lines: list[str], start: int) -> tuple[list[list[str]], int]:
    """`| … |` 블록을 파싱한다. 반환: (데이터 행, 다음 인덱스)."""
    rows: list[list[str]] = []
    i = start
    while i < len(lines):
        s = lines[i].strip()
        if not s.startswith("|"):
            break
        cells = _split_md_table_row(s)
        if not rows:
            rows.append(cells)
        elif len(rows) == 1 and _is_separator_row(cells):
            pass
        else:
            rows.append(cells)
        i += 1
    if len(rows) >= 2 and _is_separator_row(rows[1]):
        rows = [rows[0]] + rows[2:]
    return rows, i


def markdown_simulation_report_to_pdf(
    md_text: str,
    *,
    doc_title: str = "군산 SCR 시뮬레이션 보고서",
) -> tuple[bytes | None, str | None]:
    """Markdown 전체를 PDF 바이트로 변환. 실패 시 (None, 이유 메시지)."""
    font = _ensure_korean_font()
    if font is None:
        return None, (
            "한글 PDF용 글꼴을 찾지 못했습니다. "
            "Windows에서는 보통 `Windows\\Fonts\\malgun.ttf` 가 필요합니다."
        )

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "gunsan_body",
        parent=styles["Normal"],
        fontName=font,
        fontSize=8.5,
        leading=11,
        spaceAfter=3,
    )
    h1 = ParagraphStyle(
        "gunsan_h1",
        parent=styles["Heading1"],
        fontName=font,
        fontSize=15,
        leading=18,
        spaceAfter=8,
        textColor=colors.HexColor("#0f172a"),
    )
    h2 = ParagraphStyle(
        "gunsan_h2",
        parent=styles["Heading2"],
        fontName=font,
        fontSize=11.5,
        leading=14,
        spaceBefore=10,
        spaceAfter=6,
        textColor=colors.HexColor("#1e3a8a"),
    )
    h3 = ParagraphStyle(
        "gunsan_h3",
        parent=styles["Heading3"],
        fontName=font,
        fontSize=10,
        leading=12,
        spaceBefore=6,
        spaceAfter=4,
    )
    h4 = ParagraphStyle(
        "gunsan_h4",
        parent=styles["Heading4"],
        fontName=font,
        fontSize=9,
        leading=11,
        spaceBefore=4,
        spaceAfter=3,
    )
    cell_body = ParagraphStyle(
        "gunsan_cell",
        parent=styles["Normal"],
        fontName=font,
        fontSize=7,
        leading=9,
    )
    cell_tiny = ParagraphStyle(
        "gunsan_cell_tiny",
        parent=styles["Normal"],
        fontName=font,
        fontSize=6,
        leading=8,
    )

    story: list = []
    lines = md_text.replace("\r\n", "\n").split("\n")
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        s = line.strip()

        if not s:
            story.append(Spacer(1, 2))
            i += 1
            continue

        if s.startswith("#### "):
            story.append(Paragraph(_md_inline_to_reportlab_xml(s[5:].strip()), h4))
            i += 1
            continue
        if s.startswith("### "):
            story.append(Paragraph(_md_inline_to_reportlab_xml(s[4:].strip()), h3))
            i += 1
            continue
        if s.startswith("## "):
            story.append(Paragraph(_md_inline_to_reportlab_xml(s[3:].strip()), h2))
            i += 1
            continue
        if s.startswith("# ") and not s.startswith("##"):
            story.append(Paragraph(_md_inline_to_reportlab_xml(s[2:].strip()), h1))
            i += 1
            continue

        if s.startswith("|"):
            table_rows, next_i = _parse_md_table(lines, i)
            i = next_i
            if not table_rows:
                continue
            ncols = max(len(r) for r in table_rows)
            # 열 개수 맞추기
            norm = [r + [""] * (ncols - len(r)) for r in table_rows]
            use_tiny = ncols >= 5
            cstyle = cell_tiny if use_tiny else cell_body
            usable_w = A4[0] - 3 * cm
            col_w = usable_w / ncols
            data: list[list[Paragraph]] = []
            for row in norm:
                data.append(
                    [Paragraph(_md_inline_to_reportlab_xml(str(c)), cstyle) for c in row]
                )
            tbl = Table(data, colWidths=[col_w] * ncols, repeatRows=1)
            tbl.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                        ("FONTNAME", (0, 0), (-1, -1), font),
                        ("FONTSIZE", (0, 0), (-1, -1), 6 if use_tiny else 7),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#94a3b8")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]
                )
            )
            story.append(tbl)
            story.append(Spacer(1, 6))
            continue

        if s.startswith("- "):
            story.append(Paragraph("• " + _md_inline_to_reportlab_xml(s[2:].strip()), body))
            i += 1
            continue

        story.append(Paragraph(_md_inline_to_reportlab_xml(s), body))
        i += 1

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        title=doc_title,
    )
    try:
        doc.build(story)
    except Exception as exc:
        return None, f"PDF 생성 중 오류: {exc}"
    return buf.getvalue(), None
