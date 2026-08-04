#!/usr/bin/env python3
"""Render the standard 60/40 and C11 execution handbook as a polished PDF."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import re
from pathlib import Path
from urllib.parse import quote

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    HRFlowable,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "12_100万标准六四与C11策略实操手册.md"
OUTPUT = ROOT / "output" / "pdf" / "100万标准六四与C11策略实操手册.pdf"

PAGE_WIDTH, PAGE_HEIGHT = A4
LEFT_MARGIN = 18 * mm
RIGHT_MARGIN = 18 * mm
TOP_MARGIN = 18 * mm
BOTTOM_MARGIN = 17 * mm
CONTENT_WIDTH = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN

NAVY = colors.HexColor("#17324D")
BLUE = colors.HexColor("#2F5D7E")
TEAL = colors.HexColor("#168C8C")
GOLD = colors.HexColor("#D9A441")
INK = colors.HexColor("#24313D")
MUTED = colors.HexColor("#667785")
PALE_BLUE = colors.HexColor("#EAF2F7")
PALE_TEAL = colors.HexColor("#E8F5F4")
PALE_GOLD = colors.HexColor("#FFF7E4")
GRID = colors.HexColor("#C8D3DC")
WHITE = colors.white


def register_fonts() -> None:
    regular = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
    bold = Path("/System/Library/Fonts/STHeiti Medium.ttc")
    if not regular.exists() or not bold.exists():
        raise FileNotFoundError("Required macOS CJK fonts were not found")
    pdfmetrics.registerFont(TTFont("CNRegular", str(regular)))
    pdfmetrics.registerFont(TTFont("CNBold", str(bold), subfontIndex=0))
    pdfmetrics.registerFontFamily(
        "CNFamily",
        normal="CNRegular",
        bold="CNBold",
        italic="CNRegular",
        boldItalic="CNBold",
    )


def styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "CoverTitle": ParagraphStyle(
            "CoverTitle",
            parent=base["Title"],
            fontName="CNBold",
            fontSize=27,
            leading=38,
            textColor=WHITE,
            alignment=TA_LEFT,
            spaceAfter=10 * mm,
        ),
        "CoverSub": ParagraphStyle(
            "CoverSub",
            parent=base["Normal"],
            fontName="CNRegular",
            fontSize=12,
            leading=20,
            textColor=colors.HexColor("#DDEBF2"),
            spaceAfter=5 * mm,
        ),
        "CoverMeta": ParagraphStyle(
            "CoverMeta",
            parent=base["Normal"],
            fontName="CNRegular",
            fontSize=9.5,
            leading=15,
            textColor=INK,
        ),
        "H1": ParagraphStyle(
            "Heading1",
            parent=base["Heading1"],
            fontName="CNBold",
            fontSize=17,
            leading=24,
            textColor=NAVY,
            spaceBefore=7 * mm,
            spaceAfter=3.5 * mm,
            keepWithNext=True,
        ),
        "H2": ParagraphStyle(
            "Heading2",
            parent=base["Heading2"],
            fontName="CNBold",
            fontSize=13,
            leading=19,
            textColor=BLUE,
            spaceBefore=5 * mm,
            spaceAfter=2.5 * mm,
            keepWithNext=True,
        ),
        "H3": ParagraphStyle(
            "Heading3",
            parent=base["Heading3"],
            fontName="CNBold",
            fontSize=10.8,
            leading=16,
            textColor=TEAL,
            spaceBefore=3.5 * mm,
            spaceAfter=1.5 * mm,
            keepWithNext=True,
        ),
        "Body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName="CNRegular",
            fontSize=9.2,
            leading=15.2,
            textColor=INK,
            alignment=TA_LEFT,
            spaceAfter=2.6 * mm,
            wordWrap="CJK",
            allowWidows=0,
            allowOrphans=0,
        ),
        "Bullet": ParagraphStyle(
            "Bullet",
            parent=base["BodyText"],
            fontName="CNRegular",
            fontSize=9.1,
            leading=14.5,
            textColor=INK,
            leftIndent=5 * mm,
            firstLineIndent=-3.2 * mm,
            spaceAfter=1.3 * mm,
            wordWrap="CJK",
        ),
        "Quote": ParagraphStyle(
            "Quote",
            parent=base["BodyText"],
            fontName="CNBold",
            fontSize=10,
            leading=16.5,
            textColor=NAVY,
            leftIndent=4 * mm,
            rightIndent=4 * mm,
            spaceAfter=0,
            wordWrap="CJK",
        ),
        "TableHeader": ParagraphStyle(
            "TableHeader",
            parent=base["BodyText"],
            fontName="CNBold",
            fontSize=7.9,
            leading=11.4,
            textColor=WHITE,
            alignment=TA_CENTER,
            wordWrap="CJK",
        ),
        "TableBody": ParagraphStyle(
            "TableBody",
            parent=base["BodyText"],
            fontName="CNRegular",
            fontSize=7.8,
            leading=11.5,
            textColor=INK,
            wordWrap="CJK",
        ),
        "Code": ParagraphStyle(
            "Code",
            parent=base["Code"],
            fontName="CNRegular",
            fontSize=7.8,
            leading=11.5,
            textColor=colors.HexColor("#20313D"),
            leftIndent=4 * mm,
            rightIndent=4 * mm,
            backColor=colors.HexColor("#F4F7F9"),
            borderColor=GRID,
            borderWidth=0.4,
            borderPadding=4,
            spaceAfter=3 * mm,
        ),
        "Small": ParagraphStyle(
            "Small",
            parent=base["BodyText"],
            fontName="CNRegular",
            fontSize=7.5,
            leading=11,
            textColor=MUTED,
            wordWrap="CJK",
        ),
        "TOCTitle": ParagraphStyle(
            "TOCTitle",
            parent=base["Title"],
            fontName="CNBold",
            fontSize=22,
            leading=30,
            textColor=NAVY,
            spaceAfter=8 * mm,
        ),
        "TOC1": ParagraphStyle(
            "TOC1",
            parent=base["Normal"],
            fontName="CNRegular",
            fontSize=9.2,
            leading=14,
            leftIndent=0,
            firstLineIndent=0,
            textColor=INK,
        ),
        "TOC2": ParagraphStyle(
            "TOC2",
            parent=base["Normal"],
            fontName="CNRegular",
            fontSize=8,
            leading=12,
            leftIndent=6 * mm,
            firstLineIndent=0,
            textColor=MUTED,
        ),
    }


def _resolve_link(href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    resolved = (ROOT / "docs" / href).resolve().relative_to(ROOT.resolve())
    encoded = quote(resolved.as_posix())
    return (
        "https://github.com/zhuyanjun1988/cn-fund/blob/"
        f"codex/publish-fund-strategy-research/{encoded}"
    )


def inline_markup(text: str) -> str:
    rendered = escape(text, quote=False)

    def link(match: re.Match[str]) -> str:
        label = match.group(1)
        href = escape(_resolve_link(match.group(2)), quote=True)
        return f'<link href="{href}" color="#168C8C"><u>{label}</u></link>'

    rendered = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, rendered)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", rendered)
    rendered = re.sub(
        r"`([^`]+)`", r'<font name="CNRegular" color="#0D7777">\1</font>', rendered
    )
    return rendered


class AllocationBar(Flowable):
    def __init__(self, width: float = CONTENT_WIDTH, height: float = 28 * mm):
        super().__init__()
        self.width = width
        self.height = height

    def draw(self) -> None:
        canvas = self.canv
        canvas.setFont("CNBold", 9)
        canvas.setFillColor(NAVY)
        canvas.drawString(0, self.height - 7, "战略风险预算")
        y = 7
        h = 11 * mm
        segments = (
            (0.60, TEAL, "权益 60%"),
            (0.30, BLUE, "债券 30%"),
            (0.10, GOLD, "现金 10%"),
        )
        x = 0
        for fraction, color, label in segments:
            segment_width = self.width * fraction
            canvas.setFillColor(color)
            canvas.roundRect(x, y, segment_width, h, 2, fill=1, stroke=0)
            canvas.setFillColor(WHITE)
            canvas.setFont("CNBold", 8.5 if fraction > 0.15 else 7.2)
            canvas.drawCentredString(x + segment_width / 2, y + h / 2 - 3, label)
            x += segment_width


class C11Flow(Flowable):
    def __init__(self, width: float = CONTENT_WIDTH, height: float = 59 * mm):
        super().__init__()
        self.width = width
        self.height = height

    def _box(self, x: float, y: float, w: float, h: float, fill: colors.Color, lines: list[str]) -> None:
        c = self.canv
        c.setFillColor(fill)
        c.setStrokeColor(GRID)
        c.roundRect(x, y, w, h, 4, fill=1, stroke=1)
        c.setFillColor(INK)
        c.setFont("CNBold", 7.6)
        baseline = y + h - 11
        for line in lines:
            c.drawCentredString(x + w / 2, baseline, line)
            baseline -= 10

    def draw(self) -> None:
        c = self.canv
        gap = 7 * mm
        top_w = (self.width - gap * 2) / 3
        top_y = self.height - 24 * mm
        self._box(0, top_y, top_w, 18 * mm, PALE_BLUE, ["月末决策", "估值截止提前2个交易日"])
        self._box(top_w + gap, top_y, top_w, 18 * mm, PALE_TEAL, ["综合估值 V", "PE/PB历史分位均值"])
        self._box((top_w + gap) * 2, top_y, top_w, 18 * mm, PALE_GOLD, ["相对趋势 R", "500 / 红利低波 12个月"])
        c.setStrokeColor(BLUE)
        c.setLineWidth(1.1)
        c.line(top_w, top_y + 9 * mm, top_w + gap, top_y + 9 * mm)
        c.line(top_w * 2 + gap, top_y + 9 * mm, top_w * 2 + gap * 2, top_y + 9 * mm)
        bottom_y = 3 * mm
        bottom_gap = 4 * mm
        bottom_w = (self.width - bottom_gap * 2) / 3
        self._box(0, bottom_y, bottom_w, 19 * mm, PALE_TEAL, ["M60", "V <= 30% 且 R > 0", "006729提高到40%"])
        self._box(bottom_w + bottom_gap, bottom_y, bottom_w, 19 * mm, PALE_BLUE, ["保持前状态", "条件不共同确认", "不做风格交易"])
        self._box((bottom_w + bottom_gap) * 2, bottom_y, bottom_w, 19 * mm, PALE_GOLD, ["D60", "V >= 70% 且 R < 0", "021550提高到40%"])
        c.setStrokeColor(MUTED)
        c.line(self.width / 2, top_y, self.width / 2, bottom_y + 19 * mm)


class StrategyDocTemplate(BaseDocTemplate):
    def __init__(self, filename: str):
        super().__init__(
            filename,
            pagesize=A4,
            leftMargin=LEFT_MARGIN,
            rightMargin=RIGHT_MARGIN,
            topMargin=TOP_MARGIN,
            bottomMargin=BOTTOM_MARGIN,
            title="100万元标准6:4与C11基金策略实操手册",
            author="CN Fund Research Project",
            subject="Research-only fund allocation and execution handbook",
        )
        frame = Frame(
            LEFT_MARGIN,
            BOTTOM_MARGIN,
            CONTENT_WIDTH,
            PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN,
            id="body",
        )
        self.addPageTemplates(PageTemplate(id="all", frames=[frame], onPage=self._draw_page))
        self._heading_counter = 0

    def beforeDocument(self) -> None:
        # multiBuild performs more than one layout pass for the table of contents.
        # Stable bookmark identifiers are required on every pass.
        self._heading_counter = 0

    def _draw_page(self, canvas, doc) -> None:
        canvas.saveState()
        if doc.page == 1:
            canvas.setFillColor(NAVY)
            canvas.rect(0, PAGE_HEIGHT * 0.46, PAGE_WIDTH, PAGE_HEIGHT * 0.54, fill=1, stroke=0)
            canvas.setFillColor(TEAL)
            canvas.rect(0, PAGE_HEIGHT * 0.45, PAGE_WIDTH, 3 * mm, fill=1, stroke=0)
            canvas.setFillColor(colors.HexColor("#F4F7F9"))
            canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT * 0.45, fill=1, stroke=0)
        else:
            canvas.setStrokeColor(GRID)
            canvas.setLineWidth(0.5)
            canvas.line(LEFT_MARGIN, PAGE_HEIGHT - 11 * mm, PAGE_WIDTH - RIGHT_MARGIN, PAGE_HEIGHT - 11 * mm)
            canvas.setFont("CNRegular", 7.2)
            canvas.setFillColor(MUTED)
            canvas.drawString(LEFT_MARGIN, PAGE_HEIGHT - 8.5 * mm, "100万元标准6:4与C11基金策略实操手册")
            canvas.drawRightString(PAGE_WIDTH - RIGHT_MARGIN, 8.5 * mm, f"第 {doc.page} 页")
            canvas.drawString(LEFT_MARGIN, 8.5 * mm, "研究与模拟用途 · 版本1.0 · 2026-08-04")
        canvas.restoreState()

    def afterFlowable(self, flowable) -> None:
        if not isinstance(flowable, Paragraph):
            return
        if flowable.style.name not in {"Heading1", "Heading2"}:
            return
        level = 0 if flowable.style.name == "Heading1" else 1
        text = flowable.getPlainText()
        self._heading_counter += 1
        key = f"heading-{self._heading_counter}"
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(text, key, level=level, closed=False)
        self.notify("TOCEntry", (level, text, self.page, key))


@dataclass
class MarkdownRenderer:
    style: dict[str, ParagraphStyle]

    def paragraph(self, text: str) -> Paragraph:
        return Paragraph(inline_markup(text), self.style["Body"])

    def quote(self, text: str) -> Table:
        body = Paragraph(inline_markup(text), self.style["Quote"])
        table = Table([[body]], colWidths=[CONTENT_WIDTH], hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), PALE_GOLD),
                    ("BOX", (0, 0), (-1, -1), 0.6, GOLD),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        return table

    def table(self, raw_rows: list[list[str]]) -> Table:
        if not raw_rows:
            raise ValueError("table cannot be empty")
        column_count = len(raw_rows[0])
        for row in raw_rows:
            if len(row) != column_count:
                raise ValueError(f"inconsistent markdown table: {raw_rows}")
        display_rows = [raw_rows[0]] + raw_rows[2:]
        data: list[list[Paragraph]] = []
        for row_index, row in enumerate(display_rows):
            cell_style = self.style["TableHeader"] if row_index == 0 else self.style["TableBody"]
            data.append([Paragraph(inline_markup(cell), cell_style) for cell in row])
        weights = []
        for column in range(column_count):
            longest = max(len(row[column]) for row in display_rows)
            weights.append(max(7, min(longest, 34)))
        total = sum(weights)
        widths = [CONTENT_WIDTH * weight / total for weight in weights]
        table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        commands = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("GRID", (0, 0), (-1, -1), 0.35, GRID),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        for row_index in range(1, len(data)):
            background = colors.white if row_index % 2 else colors.HexColor("#F5F8FA")
            commands.append(("BACKGROUND", (0, row_index), (-1, row_index), background))
        table.setStyle(TableStyle(commands))
        return table

    def render(self, markdown: str) -> list[Flowable]:
        lines = markdown.splitlines()
        story: list[Flowable] = []
        paragraph_buffer: list[str] = []
        started = False
        index = 0

        def flush_paragraph() -> None:
            if paragraph_buffer:
                story.append(self.paragraph(" ".join(part.strip() for part in paragraph_buffer)))
                paragraph_buffer.clear()

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if not started:
                if stripped == "<!-- PAGEBREAK -->":
                    started = True
                index += 1
                continue
            if stripped == "<!-- PAGEBREAK -->":
                flush_paragraph()
                story.append(PageBreak())
                index += 1
                continue
            if stripped == "[[ALLOCATION_BAR]]":
                flush_paragraph()
                story.extend([AllocationBar(), Spacer(1, 3 * mm)])
                index += 1
                continue
            if stripped == "[[C11_FLOW]]":
                flush_paragraph()
                story.extend([C11Flow(), Spacer(1, 3 * mm)])
                index += 1
                continue
            if stripped.startswith("```"):
                flush_paragraph()
                code_lines: list[str] = []
                index += 1
                while index < len(lines) and not lines[index].strip().startswith("```"):
                    code_lines.append(lines[index])
                    index += 1
                story.append(Preformatted("\n".join(code_lines), self.style["Code"]))
                index += 1
                continue
            if stripped.startswith("## "):
                flush_paragraph()
                story.append(Paragraph(inline_markup(stripped[3:]), self.style["H1"]))
                index += 1
                continue
            if stripped.startswith("### "):
                flush_paragraph()
                story.append(Paragraph(inline_markup(stripped[4:]), self.style["H2"]))
                index += 1
                continue
            if stripped.startswith("#### "):
                flush_paragraph()
                story.append(Paragraph(inline_markup(stripped[5:]), self.style["H3"]))
                index += 1
                continue
            if stripped.startswith("> "):
                flush_paragraph()
                story.extend([self.quote(stripped[2:]), Spacer(1, 3 * mm)])
                index += 1
                continue
            if stripped.startswith("|"):
                flush_paragraph()
                raw_rows: list[list[str]] = []
                while index < len(lines) and lines[index].strip().startswith("|"):
                    cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                    raw_rows.append(cells)
                    index += 1
                story.extend([self.table(raw_rows), Spacer(1, 3.5 * mm)])
                continue
            if re.match(r"^[-*] ", stripped):
                flush_paragraph()
                story.append(Paragraph("• " + inline_markup(stripped[2:]), self.style["Bullet"]))
                index += 1
                continue
            number_match = re.match(r"^(\d+)\.\s+(.*)$", stripped)
            if number_match:
                flush_paragraph()
                story.append(
                    Paragraph(
                        f"{number_match.group(1)}. " + inline_markup(number_match.group(2)),
                        self.style["Bullet"],
                    )
                )
                index += 1
                continue
            if stripped == "---":
                flush_paragraph()
                story.append(HRFlowable(width="100%", thickness=0.5, color=GRID, spaceBefore=3, spaceAfter=6))
                index += 1
                continue
            if not stripped:
                flush_paragraph()
                index += 1
                continue
            paragraph_buffer.append(stripped)
            index += 1
        flush_paragraph()
        return story


def build_story(style: dict[str, ParagraphStyle], markdown: str) -> list[Flowable]:
    story: list[Flowable] = [
        Spacer(1, 44 * mm),
        Paragraph("100万元标准6:4与C11<br/>基金策略实操手册", style["CoverTitle"]),
        Paragraph(
            "从资产角色、初始建仓和50周定投，到月末再平衡、C11状态切换和场外基金结算",
            style["CoverSub"],
        ),
        Spacer(1, 10 * mm),
        AllocationBar(height=25 * mm),
        Spacer(1, 25 * mm),
    ]
    meta = Table(
        [
            ["版本", "1.0", "研究截止", "2026-08-04"],
            ["初始资金", "1,000,000元", "适用范围", "境内人民币场外公募基金"],
            ["默认建议", "标准6:4", "C11状态", "研究候选，未获实盘批准"],
        ],
        colWidths=[22 * mm, 37 * mm, 27 * mm, CONTENT_WIDTH - 86 * mm],
        hAlign="LEFT",
    )
    meta.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "CNRegular"),
                ("FONTNAME", (0, 0), (0, -1), "CNBold"),
                ("FONTNAME", (2, 0), (2, -1), "CNBold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                ("BACKGROUND", (0, 0), (-1, -1), WHITE),
                ("BOX", (0, 0), (-1, -1), 0.5, GRID),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, GRID),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend(
        [
            meta,
            Spacer(1, 8 * mm),
            Table(
                [[Paragraph("研究与模拟用途。历史表现不代表未来；执行前必须重新核验费率、限购、申赎和个人风险承受能力。", style["Quote"]) ]],
                colWidths=[CONTENT_WIDTH],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), PALE_GOLD),
                        ("BOX", (0, 0), (-1, -1), 0.6, GOLD),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ]
                ),
            ),
            PageBreak(),
            Paragraph("目录", style["TOCTitle"]),
        ]
    )
    toc = TableOfContents()
    toc.levelStyles = [style["TOC1"], style["TOC2"]]
    toc.dotsMinLevel = 0
    story.extend([toc, PageBreak()])
    story.extend(MarkdownRenderer(style).render(markdown))
    return story


def main() -> int:
    register_fonts()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    markdown = SOURCE.read_text(encoding="utf-8")
    style = styles()
    doc = StrategyDocTemplate(str(OUTPUT))
    doc.multiBuild(build_story(style, markdown))
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
