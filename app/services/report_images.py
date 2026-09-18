"""Deterministic grade graphics; all data can be frozen for durable delivery."""

import json
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

IMAGE_PREFIX = "mesh-grade-image-v1:"


@dataclass(frozen=True)
class GradeRow:
    subject: str
    marks: str
    average: str
    delta: str
    direction: str
    needed: str


@dataclass(frozen=True)
class GradeDocument:
    student: str
    title: str
    periods: list[str]
    rows: list[GradeRow]
    marks_label: str
    legend: str
    warning: str = ""
    empty_text: str = "Оценок и средних в МЭШ не найдено."

    def freeze(self) -> str:
        return IMAGE_PREFIX + json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def thaw(cls, content: str) -> "GradeDocument":
        data = json.loads(content.removeprefix(IMAGE_PREFIX))
        data["rows"] = [GradeRow(**row) for row in data["rows"]]
        return cls(**data)


def font_files(custom: Path | None = None) -> tuple[Path, Path]:
    candidates = (
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
        (
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ),
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
    )
    if custom:
        if not custom.is_file():
            raise ValueError("REPORT_FONT_PATH must point to a Cyrillic TrueType font")
        bold = custom.with_name(custom.stem + "-Bold" + custom.suffix)
        return custom, bold if bold.is_file() else custom
    for regular, bold in candidates:
        if regular.is_file():
            return regular, bold if bold.is_file() else regular
    raise ValueError("Install fonts-dejavu-core or set REPORT_FONT_PATH")


def wrapped(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Pixel-based wrapping also handles long subject names and single words."""
    lines: list[str] = []
    for paragraph in text.replace("\r", "").split("\n"):
        current = ""
        for word in paragraph.split():
            proposed = (current + " " + word).strip()
            if font.getlength(proposed) <= width:
                current = proposed
                continue
            if current:
                lines.append(current)
                current = ""
            for char in word:
                if current and font.getlength(current + char) > width:
                    lines.append(current)
                    current = ""
                current += char
        lines.append(current)
    return lines or [""]


def render_image(document: GradeDocument, custom_font: Path | None = None) -> bytes:
    regular, bold = font_files(custom_font)
    title_font = ImageFont.truetype(str(bold), 42)
    name_font = ImageFont.truetype(str(bold), 28)
    body_font = ImageFont.truetype(str(regular), 27)
    small_font = ImageFont.truetype(str(regular), 23)
    mean_font = ImageFont.truetype(str(bold), 35)
    count_font = ImageFont.truetype(str(bold), 32)
    navy, muted = "#142744", "#566780"
    green, red = "#16733B", "#CE443D"
    width, margin = 1200, 32
    name_lines = wrapped("Оценки · " + document.student, title_font, width - 2 * margin)
    title_lines = wrapped(document.title, body_font, width - 2 * margin)
    period_lines = wrapped(
        "Триместр " + "; ".join(document.periods) if document.periods else "Период МЭШ не указан",
        small_font,
        width - 2 * margin,
    )
    warning_lines = (
        wrapped(document.warning, small_font, width - 2 * margin) if document.warning else []
    )
    header_height = 40 + len(name_lines) * 50 + len(title_lines) * 34 + len(period_lines) * 29
    header_height += len(warning_lines) * 29 + 16
    row_layout = []
    for row in document.rows:
        subject = wrapped(row.subject, name_font, 390)
        marks = wrapped(row.marks, body_font, 245)
        height = max(72, 22 + max(len(subject) * 34, len(marks) * 33))
        row_layout.append((row, subject, marks, height))
    footer_lines = wrapped(
        "↑ ↓ → — изменение среднего за неделю\n"
        + document.legend
        + "\nОриентир, не итоговая оценка",
        small_font,
        width - 2 * margin,
    )
    height = (
        header_height + 58 + sum(row[3] + 6 for row in row_layout) + len(footer_lines) * 29 + 45
    )
    if not row_layout:
        height += 80
    # Telegram photos must stay within the combined dimension limit.
    if width + height > 10000:
        raise ValueError("Report image too tall; use REPORT_FORMAT=text")
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    y = 24
    for line in name_lines:
        draw.text((margin, y), line, font=title_font, fill=navy)
        y += 50
    for line in title_lines:
        draw.text((margin, y), line, font=body_font, fill=muted)
        y += 34
    for line in period_lines:
        draw.text((margin, y), line, font=small_font, fill=muted)
        y += 29
    for line in warning_lines:
        draw.text((margin, y), line, font=small_font, fill=red)
        y += 29
    y = header_height
    draw.rounded_rectangle((margin, y, width - margin, y + 50), radius=12, fill="#E7F2FD")
    for x, text in ((48, "Предмет"), (455, document.marks_label), (735, "Среднее"), (1070, "До 5")):
        draw.text((x, y + 9), text, font=body_font, fill=navy)
    y += 58
    for row, subject, marks, row_height in row_layout:
        draw.rounded_rectangle(
            (margin, y, width - margin, y + row_height), radius=12, fill="#F0F6FC"
        )
        for index, line in enumerate(subject):
            draw.text((48, y + 11 + index * 34), line, font=name_font, fill=navy)
        for index, line in enumerate(marks):
            draw.text((455, y + 11 + index * 33), line, font=body_font, fill=muted)
        center = y + row_height // 2
        draw.text((735, center), row.average, anchor="lm", font=mean_font, fill=navy)
        color = green if row.direction == "UP" else red if row.direction == "DOWN" else muted
        draw.text((860, center), row.delta, anchor="lm", font=small_font, fill=color)
        met = row.needed == "0"
        fill = "#D7F0DA" if met else "#FEEBB7" if row.needed != "—" else "#E6EBF1"
        draw.rounded_rectangle((1070, center - 24, 1150, center + 24), radius=22, fill=fill)
        draw.text(
            (1110, center), row.needed, anchor="mm", font=count_font, fill=green if met else navy
        )
        y += row_height + 6
    if not row_layout:
        for line in wrapped(document.empty_text, body_font, width - 2 * margin):
            draw.text((margin, y + 12), line, font=body_font, fill=muted)
            y += 34
        y += 20
    y += 20
    for line in footer_lines:
        draw.text((margin, y), line, font=small_font, fill=muted)
        y += 29
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
