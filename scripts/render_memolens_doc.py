from __future__ import annotations

import html
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, StyleSheet1
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    HRFlowable,
    Image as RLImage,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)


PAGE_WIDTH, PAGE_HEIGHT = A4
LEFT_MARGIN = 18 * mm
RIGHT_MARGIN = 18 * mm
TOP_MARGIN = 16 * mm
BOTTOM_MARGIN = 18 * mm

PAPER = HexColor("#F6F1E8")
INK = HexColor("#182230")
MUTED = HexColor("#5F6B78")
ACCENT = HexColor("#0F4C5C")
ACCENT_WARM = HexColor("#B86D49")
LINE = HexColor("#D8CFBE")
SOFT = HexColor("#FBF8F2")
QUOTE_BG = HexColor("#E9F0F2")
CODE_BG = HexColor("#162033")
CODE_FG = HexColor("#F6F7FB")

BODY_FONT = "MemoSongtiRegular"
BOLD_FONT = "MemoSongtiBold"
DISPLAY_FONT = "MemoSongtiBlack"
CODE_FONT = "MemoMono"

TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
BULLET_RE = re.compile(r"^-\s+(.*)$")
ORDERED_RE = re.compile(r"^\d+\.\s+(.*)$")
HEADING_RE = re.compile(r"^(#{2,6})\s+(.*)$")
QUOTE_RE = re.compile(r"^>\s?(.*)$")
IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")


@dataclass
class Block:
    kind: str
    text: str = ""
    level: int = 0
    items: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    ordered: bool = False


class CoverFlowable(Flowable):
    def __init__(self, metadata: dict[str, str], image_path: Path, styles: StyleSheet1) -> None:
        super().__init__()
        self.metadata = metadata
        self.image_path = image_path
        self.styles = styles
        self.width = 0
        self.height = 0

    def wrap(self, avail_width: float, avail_height: float):
        self.width = avail_width
        self.height = avail_height
        return avail_width, avail_height

    def draw(self) -> None:
        canvas = self.canv
        image_width = self.width * 0.54
        panel_x = image_width + (6 * mm)
        panel_width = self.width - image_width - (6 * mm)

        if self.image_path.exists():
            canvas.drawImage(
                str(self.image_path),
                0,
                0,
                width=image_width,
                height=self.height,
                preserveAspectRatio=True,
                anchor="c",
                mask="auto",
            )

        canvas.saveState()
        canvas.setFillColor(HexColor("#172032"))
        canvas.roundRect(panel_x, 0, panel_width, self.height, 16, stroke=0, fill=1)
        canvas.restoreState()

        title = Paragraph(
            self.metadata.get("title", "MemoLens"),
            self.styles["cover_title"],
        )
        subtitle = Paragraph(
            self.metadata.get("subtitle", ""),
            self.styles["cover_subtitle"],
        )
        summary = Paragraph(
            self.metadata.get(
                "cover_summary",
                (
                    "一款围绕本地照片库构建的个人记忆管理智能体原型。"
                    "它允许用户直接描述记忆，再由系统完成理解、检索、筛选与表达组织。"
                ),
            ),
            self.styles["cover_summary"],
        )

        cursor_y = self.height - 20 * mm
        for flowable in [
            Paragraph("MemoLens / Product Brief", self.styles["cover_eyebrow"]),
            Spacer(1, 5 * mm),
            title,
            Spacer(1, 2.5 * mm),
            subtitle,
            Spacer(1, 8 * mm),
            summary,
            Spacer(1, 10 * mm),
        ]:
            width, height = flowable.wrap(panel_width - 18 * mm, self.height)
            cursor_y -= height
            flowable.drawOn(canvas, panel_x + 9 * mm, cursor_y)

        cards = [
            ("文档类型", self.metadata.get("doc_type", "产品说明文档")),
            ("当前定位", self.metadata.get("positioning", "本地优先的照片检索原型")),
            ("模型策略", self.metadata.get("stack", "Local + API Hybrid")),
            ("版本日期", self.metadata.get("date", "")),
        ]

        card_height = 19 * mm
        card_gap = 3 * mm
        card_y = cursor_y - 5 * mm
        for label, value in cards:
            card_y -= card_height
            canvas.saveState()
            canvas.setFillColor(colors.Color(1, 1, 1, alpha=0.08))
            canvas.roundRect(
                panel_x + 9 * mm,
                card_y,
                panel_width - 18 * mm,
                card_height,
                10,
                stroke=0,
                fill=1,
            )
            canvas.restoreState()

            label_para = Paragraph(label, self.styles["meta_label"])
            value_para = Paragraph(value, self.styles["meta_value"])
            label_para.wrapOn(canvas, panel_width - 24 * mm, card_height)
            label_para.drawOn(canvas, panel_x + 12 * mm, card_y + 11 * mm)
            value_para.wrapOn(canvas, panel_width - 24 * mm, card_height)
            value_para.drawOn(canvas, panel_x + 12 * mm, card_y + 4 * mm)
            card_y -= card_gap


def register_fonts() -> None:
    global BODY_FONT, BOLD_FONT, DISPLAY_FONT, CODE_FONT

    songti_path = Path("/System/Library/Fonts/Supplemental/Songti.ttc")
    mono_path = Path("/System/Library/Fonts/SFNSMono.ttf")

    try:
        pdfmetrics.registerFont(TTFont(BODY_FONT, str(songti_path), subfontIndex=6))
        pdfmetrics.registerFont(TTFont(BOLD_FONT, str(songti_path), subfontIndex=1))
        pdfmetrics.registerFont(TTFont(DISPLAY_FONT, str(songti_path), subfontIndex=0))
    except Exception:
        BODY_FONT = "STSong-Light"
        BOLD_FONT = "STSong-Light"
        DISPLAY_FONT = "STSong-Light"
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    try:
        pdfmetrics.registerFont(TTFont(CODE_FONT, str(mono_path)))
    except Exception:
        CODE_FONT = "Courier"


def build_styles() -> StyleSheet1:
    styles = StyleSheet1()
    styles.add(
        ParagraphStyle(
            name="body",
            fontName=BODY_FONT,
            fontSize=11.2,
            leading=18,
            textColor=INK,
            spaceAfter=4,
            wordWrap="CJK",
        )
    )
    styles.add(
        ParagraphStyle(
            name="section",
            parent=styles["body"],
            fontName=DISPLAY_FONT,
            fontSize=18.5,
            leading=24,
            textColor=ACCENT,
            spaceBefore=18,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="subsection",
            parent=styles["body"],
            fontName=BOLD_FONT,
            fontSize=13.3,
            leading=18,
            textColor=INK,
            spaceBefore=10,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="quote",
            parent=styles["body"],
            leftIndent=0,
            rightIndent=0,
            textColor=HexColor("#17324A"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="toc_title",
            parent=styles["section"],
            spaceBefore=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="toc_item",
            parent=styles["body"],
            fontName=BOLD_FONT,
            fontSize=11,
            leading=16,
            spaceAfter=5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="list_item",
            parent=styles["body"],
            spaceAfter=5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="cover_eyebrow",
            fontName=BOLD_FONT,
            fontSize=8.3,
            leading=10,
            textColor=colors.Color(1, 1, 1, alpha=0.78),
            wordWrap="CJK",
        )
    )
    styles.add(
        ParagraphStyle(
            name="cover_title",
            fontName="Helvetica-Bold",
            fontSize=29,
            leading=33,
            textColor=colors.white,
            splitLongWords=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="cover_subtitle",
            fontName=BOLD_FONT,
            fontSize=15.2,
            leading=20,
            textColor=colors.white,
            wordWrap="CJK",
        )
    )
    styles.add(
        ParagraphStyle(
            name="cover_summary",
            fontName=BODY_FONT,
            fontSize=10.4,
            leading=17,
            textColor=colors.Color(1, 1, 1, alpha=0.86),
            wordWrap="CJK",
        )
    )
    styles.add(
        ParagraphStyle(
            name="meta_label",
            fontName=BODY_FONT,
            fontSize=8.3,
            leading=10,
            textColor=colors.Color(1, 1, 1, alpha=0.74),
            wordWrap="CJK",
        )
    )
    styles.add(
        ParagraphStyle(
            name="meta_value",
            fontName=BOLD_FONT,
            fontSize=10.2,
            leading=12,
            textColor=colors.white,
            wordWrap="CJK",
        )
    )
    return styles


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    metadata: dict[str, str] = {}
    end_index = None
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            end_index = index
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"')

    if end_index is None:
        return {}, text

    content = "\n".join(lines[end_index + 1 :]).lstrip()
    return metadata, content


def parse_markdown(content: str) -> list[Block]:
    lines = content.splitlines()
    blocks: list[Block] = []
    index = 0

    def is_block_start(value: str) -> bool:
        stripped = value.strip()
        if not stripped:
            return True
        return bool(
            HEADING_RE.match(stripped)
            or QUOTE_RE.match(stripped)
            or BULLET_RE.match(stripped)
            or ORDERED_RE.match(stripped)
            or stripped.startswith("```")
        )

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        heading_match = HEADING_RE.match(stripped)
        if heading_match:
            blocks.append(
                Block(
                    kind="heading",
                    level=len(heading_match.group(1)),
                    text=heading_match.group(2).strip(),
                )
            )
            index += 1
            continue

        image_match = IMAGE_RE.match(stripped)
        if image_match:
            blocks.append(
                Block(
                    kind="image",
                    text=image_match.group(1).strip(),
                    items=[image_match.group(2).strip().strip('"')],
                )
            )
            index += 1
            continue

        if stripped.startswith("```"):
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index].rstrip("\n"))
                index += 1
            index += 1
            blocks.append(Block(kind="code", text="\n".join(code_lines).strip("\n")))
            continue

        if "|" in line and index + 1 < len(lines) and TABLE_SEPARATOR_RE.match(lines[index + 1].strip()):
            table_lines = [line]
            index += 2
            while index < len(lines) and "|" in lines[index]:
                table_lines.append(lines[index])
                index += 1
            rows = [split_table_row(item) for item in table_lines]
            blocks.append(Block(kind="table", rows=rows))
            continue

        if QUOTE_RE.match(stripped):
            quote_lines: list[str] = []
            while index < len(lines) and QUOTE_RE.match(lines[index].strip()):
                quote_lines.append(QUOTE_RE.match(lines[index].strip()).group(1))
                index += 1
            blocks.append(Block(kind="quote", text=" ".join(quote_lines).strip()))
            continue

        if BULLET_RE.match(stripped):
            items: list[str] = []
            while index < len(lines):
                current = lines[index].strip()
                match = BULLET_RE.match(current)
                if not match:
                    break
                items.append(match.group(1).strip())
                index += 1
            blocks.append(Block(kind="list", items=items, ordered=False))
            continue

        if ORDERED_RE.match(stripped):
            items = []
            while index < len(lines):
                current = lines[index].strip()
                match = ORDERED_RE.match(current)
                if not match:
                    break
                items.append(match.group(1).strip())
                index += 1
            blocks.append(Block(kind="list", items=items, ordered=True))
            continue

        paragraph_lines = [stripped]
        index += 1
        while index < len(lines):
            current = lines[index]
            if not current.strip():
                break
            if is_block_start(current):
                break
            if "|" in current and index + 1 < len(lines) and TABLE_SEPARATOR_RE.match(lines[index + 1].strip()):
                break
            paragraph_lines.append(current.strip())
            index += 1
        blocks.append(Block(kind="paragraph", text=" ".join(paragraph_lines).strip()))

    return blocks


def split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def format_inline(text: str) -> str:
    code_fragments: list[str] = []

    def replace_code(match: re.Match[str]) -> str:
        code_text = html.escape(match.group(1))
        code_fragments.append(f'<font name="{CODE_FONT}">{code_text}</font>')
        return f"@@CODE{len(code_fragments) - 1}@@"

    text = re.sub(r"`([^`]+)`", replace_code, text)
    text = html.escape(text)
    text = re.sub(
        r"\*\*([^*]+)\*\*",
        lambda match: f'<font name="{BOLD_FONT}">{match.group(1)}</font>',
        text,
    )
    text = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", text)

    for index, fragment in enumerate(code_fragments):
        text = text.replace(f"@@CODE{index}@@", fragment)

    return text


def make_quote(text: str, styles: StyleSheet1, width: float):
    paragraph = Paragraph(format_inline(text), styles["quote"])
    table = Table([[paragraph]], colWidths=[width])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), QUOTE_BG),
                ("LINEBEFORE", (0, 0), (0, -1), 4, ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def make_code_block(text: str, styles: StyleSheet1, width: float):
    code_font = BODY_FONT if any(ord(char) > 127 for char in text) else CODE_FONT
    pre = Preformatted(
        text,
        ParagraphStyle(
            "code",
            fontName=code_font,
            fontSize=9.5,
            leading=13.5,
            textColor=CODE_FG,
        ),
    )
    box = Table([[pre]], colWidths=[width])
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return box


def make_list_flowables(items: list[str], ordered: bool, styles: StyleSheet1) -> list[Paragraph]:
    flowables: list[Paragraph] = []
    for index, item in enumerate(items, start=1):
        bullet = f"{index}." if ordered else "•"
        bullet_color = ACCENT if ordered else ACCENT_WARM
        text = (
            f'<font name="{BOLD_FONT}" color="{bullet_color}">{bullet}</font>'
            f"&nbsp;&nbsp;{format_inline(item)}"
        )
        flowables.append(Paragraph(text, styles["list_item"]))
    return flowables


def strip_markdown(text: str) -> str:
    return re.sub(r"[*`]", "", text)


def make_table(block: Block, styles: StyleSheet1, width: float):
    rows = [[Paragraph(format_inline(cell), styles["body"]) for cell in row] for row in block.rows]
    column_count = len(block.rows[0])
    if column_count == 2:
        col_widths = [width * 0.3, width * 0.7]
    elif column_count == 3:
        col_widths = [width * 0.22, width * 0.29, width * 0.49]
    elif column_count == 4:
        col_widths = [width * 0.18, width * 0.24, width * 0.28, width * 0.30]
    else:
        col_widths = [width / column_count for _ in range(column_count)]

    table = Table(rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HexColor("#E7ECEF")),
                ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#14324B")),
                ("FONTNAME", (0, 0), (-1, 0), BOLD_FONT),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, LINE),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.Color(0, 0, 0, alpha=0.08)),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def make_image(block: Block, styles: StyleSheet1, source_path: Path, width: float):
    if not block.items:
        return []

    raw_path = block.items[0]
    image_path = Path(raw_path).expanduser()
    if not image_path.is_absolute():
        image_path = (source_path.parent / image_path).resolve()
    if not image_path.exists():
        return [
            make_quote(
                f"Image not found: {raw_path}",
                styles,
                width,
            )
        ]

    image = RLImage(str(image_path))
    max_height = 130 * mm
    scale = min(width / image.imageWidth, max_height / image.imageHeight, 1)
    image.drawWidth = image.imageWidth * scale
    image.drawHeight = image.imageHeight * scale

    flowables = [image]
    if block.text:
        caption_style = ParagraphStyle(
            "image_caption",
            parent=styles["body"],
            fontSize=9.4,
            leading=13,
            textColor=MUTED,
            alignment=TA_LEFT,
            spaceBefore=4,
            spaceAfter=4,
        )
        flowables.append(Paragraph(format_inline(block.text), caption_style))
    return flowables


def build_story(metadata: dict[str, str], blocks: list[Block], styles: StyleSheet1, source_path: Path):
    story = []
    cover_image = source_path.parent / metadata.get("cover_image", "")
    story.append(CoverFlowable(metadata, cover_image, styles))
    story.append(PageBreak())

    section_titles = [block.text for block in blocks if block.kind == "heading" and block.level == 2]
    story.append(Paragraph("目录", styles["toc_title"]))
    story.append(HRFlowable(width="100%", thickness=0.8, color=LINE, spaceBefore=0, spaceAfter=10))
    toc_items = [f"{index}. {title}" for index, title in enumerate(section_titles, start=1)]
    for item in toc_items:
        story.append(Paragraph(format_inline(item), styles["toc_item"]))
    story.append(PageBreak())

    content_width = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN
    for block in blocks:
        if block.kind == "heading":
            if block.level == 2:
                story.append(Paragraph(block.text, styles["section"]))
                story.append(HRFlowable(width="100%", thickness=0.8, color=LINE, spaceBefore=0, spaceAfter=8))
            elif block.level == 3:
                story.append(Paragraph(block.text, styles["subsection"]))
            continue

        if block.kind == "paragraph":
            story.append(Paragraph(format_inline(block.text), styles["body"]))
            story.append(Spacer(1, 2))
            continue

        if block.kind == "quote":
            story.append(make_quote(block.text, styles, content_width))
            story.append(Spacer(1, 4))
            continue

        if block.kind == "list":
            story.extend(make_list_flowables(block.items, block.ordered, styles))
            story.append(Spacer(1, 6))
            continue

        if block.kind == "code":
            story.append(make_code_block(block.text, styles, content_width))
            story.append(Spacer(1, 6))
            continue

        if block.kind == "table":
            story.append(make_table(block, styles, content_width))
            story.append(Spacer(1, 8))
            continue

        if block.kind == "image":
            story.extend(make_image(block, styles, source_path, content_width))
            story.append(Spacer(1, 8))
            continue

    return story


def draw_body_page(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.Color(0, 0, 0, alpha=0.08))
    canvas.line(doc.leftMargin, PAGE_HEIGHT - 11 * mm, PAGE_WIDTH - doc.rightMargin, PAGE_HEIGHT - 11 * mm)
    canvas.setFont(BODY_FONT, 9)
    canvas.setFillColor(MUTED)
    canvas.drawString(doc.leftMargin, 10 * mm, getattr(doc, "footer_title", "MemoLens 产品说明文档"))
    canvas.drawRightString(PAGE_WIDTH - doc.rightMargin, 10 * mm, str(max(canvas.getPageNumber() - 1, 1)))
    canvas.restoreState()


def render(source_path: Path, output_path: Path) -> None:
    register_fonts()
    styles = build_styles()

    metadata, content = split_front_matter(source_path.read_text(encoding="utf-8"))
    blocks = parse_markdown(content)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=LEFT_MARGIN,
        rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        title=metadata.get("title", "MemoLens"),
        author="OpenAI Codex",
    )
    doc.footer_title = metadata.get("doc_type", "MemoLens 产品说明文档")

    frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id="body",
    )
    doc.addPageTemplates(
        [
            PageTemplate(id="cover", frames=[frame], autoNextPageTemplate="body"),
            PageTemplate(id="body", frames=[frame], onPage=draw_body_page),
        ]
    )

    story = build_story(metadata, blocks, styles, source_path)
    doc.build(story)


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: render_memolens_doc.py <input.md> <output.pdf>", file=sys.stderr)
        return 1

    source_path = Path(sys.argv[1]).resolve()
    output_path = Path(sys.argv[2]).resolve()

    if not source_path.exists():
        print(f"Missing source file: {source_path}", file=sys.stderr)
        return 1

    render(source_path, output_path)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
