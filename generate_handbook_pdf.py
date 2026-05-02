"""Render HANDBOOK.md → HANDBOOK.pdf using the same FPDF style as the other reports."""

import os
import re

from fpdf import FPDF

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = "/Library/Fonts/Arial Unicode.ttf"


class HandbookPDF(FPDF):
    def __init__(self):
        super().__init__()
        self.add_font("Arial", "", FONT_PATH)
        self.add_font("Arial", "B", FONT_PATH)
        self.set_auto_page_break(auto=True, margin=18)
        self.add_page()

    def header(self):
        self.set_font("Arial", "B", 9)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, "ORB Monitor - Operator Handbook V2", align="R")
        self.ln(2)
        self.set_draw_color(200, 200, 200)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-14)
        self.set_font("Arial", "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, f"Page {self.page_no()}  |  Nifty ORB Monitor", align="C")

    def cover(self):
        self.set_y(70)
        self.set_x(10)
        self.set_font("Arial", "B", 24)
        self.set_text_color(30, 80, 160)
        self.cell(0, 14, "Nifty ORB Alert System", align="C", ln=1)
        self.set_x(10)
        self.set_font("Arial", "B", 16)
        self.cell(0, 10, "Operator Handbook - V2", align="C", ln=1)
        self.ln(8)
        self.set_x(10)
        self.set_font("Arial", "", 12)
        self.set_text_color(80, 80, 80)
        self.cell(0, 7, "5-min tracking  |  ITM-1 strikes  |  1-tap LIMIT orders", align="C", ln=1)
        self.ln(20)
        self.set_x(10)
        self.set_font("Arial", "", 10)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, "May 2026", align="C", ln=1)
        self.add_page()

    def _reset_x(self):
        self.set_x(self.l_margin)

    @staticmethod
    def _asciify(text):
        """Replace unicode chars not in the base Courier font with ASCII equivalents."""
        replacements = {
            "→": "->", "←": "<-", "≤": "<=", "≥": ">=",
            "×": "x", "₹": "Rs.", "—": "--", "–": "-",
            "“": '"', "”": '"', "‘": "'", "’": "'",
            "✓": "[ok]", "✗": "[x]", "•": "*",
        }
        for u, a in replacements.items():
            text = text.replace(u, a)
        return text

    def h1(self, text):
        self.ln(4)
        self._reset_x()
        self.set_font("Arial", "B", 15)
        self.set_text_color(30, 80, 160)
        self.multi_cell(0, 9, text)
        self.set_draw_color(30, 80, 160)
        self.set_line_width(0.5)
        self.line(10, self.get_y(), 200, self.get_y())
        self.set_line_width(0.2)
        self.ln(3)

    def h2(self, text):
        self.ln(2)
        self._reset_x()
        self.set_font("Arial", "B", 12)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 7, text)
        self.ln(1)

    def body(self, text):
        self._reset_x()
        self.set_font("Arial", "", 10)
        self.set_text_color(50, 50, 50)
        self.multi_cell(0, 6, text)
        self.ln(1)

    def bullet(self, text):
        self.set_font("Arial", "", 10)
        self.set_text_color(50, 50, 50)
        self.set_x(16)
        self.cell(6, 6, "->", ln=0)
        self.multi_cell(0, 6, text)
        self._reset_x()

    def code_block(self, text):
        self.ln(1)
        self._reset_x()
        self.set_fill_color(240, 243, 248)
        self.set_draw_color(200, 210, 225)
        lines = text.strip("\n").split("\n") or [""]
        line_h = 5
        total_h = len(lines) * line_h + 8
        y = self.get_y()
        if y + total_h > 270:
            self.add_page()
            y = self.get_y()
        self.rect(10, y, 190, total_h, "DF")
        self.set_xy(13, y + 4)
        self.set_font("Courier", "", 8.5)
        self.set_text_color(30, 30, 80)
        for line in lines:
            self.set_x(13)
            line = self._asciify(line)
            if len(line) > 95:
                line = line[:92] + "..."
            self.cell(0, line_h, line, ln=1)
        self.ln(2)
        self._reset_x()

    def table(self, headers, rows, col_widths):
        self.ln(2)
        self.set_fill_color(30, 80, 160)
        self.set_text_color(255, 255, 255)
        self.set_font("Arial", "B", 9)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 7, f"  {h}", border=1, fill=True)
        self.ln()
        for ri, row in enumerate(rows):
            fill = ri % 2 == 0
            self.set_fill_color(245, 248, 255) if fill else self.set_fill_color(255, 255, 255)
            self.set_text_color(40, 40, 40)
            self.set_font("Arial", "", 9)
            row_h = 6
            # Compute heights for each cell to handle wrapping
            cell_lines = [self._wrap_lines(str(c), col_widths[i] - 4) for i, c in enumerate(row)]
            max_lines = max(len(cl) for cl in cell_lines)
            row_h = 6 * max_lines
            x_start = self.get_x()
            y_start = self.get_y()
            if y_start + row_h > 275:
                self.add_page()
                y_start = self.get_y()
            for i, cl in enumerate(cell_lines):
                self.set_xy(x_start, y_start)
                self.cell(col_widths[i], row_h, "", border=1, fill=fill)
                self.set_xy(x_start + 2, y_start + 1)
                self.set_font("Arial", "", 9)
                self.multi_cell(col_widths[i] - 4, 5, str(row[i]))
                x_start += col_widths[i]
            self.set_xy(10, y_start + row_h)
        self.ln(2)

    def _wrap_lines(self, text, width_mm):
        # Approximate chars-per-mm at font size 9 ≈ 0.5
        chars = max(1, int(width_mm / 1.7))
        words = text.split()
        lines, cur = [], ""
        for w in words:
            if len(cur) + len(w) + 1 <= chars:
                cur = (cur + " " + w).strip()
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    def render_inline(self, text):
        """Strip basic markdown inline markers (**bold**, *italic*, `code`) for plain rendering."""
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
        # Arial Unicode on macOS lacks the rupee glyph; substitute "Rs."
        text = text.replace("₹", "Rs.")
        return text


def parse_markdown(md_path):
    """Yield (kind, payload) tokens from a simple markdown file."""
    with open(md_path) as f:
        lines = f.read().split("\n")

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("# "):
            yield ("h1", stripped[2:].strip())
            i += 1
            continue

        if stripped.startswith("## "):
            yield ("h2", stripped[3:].strip())
            i += 1
            continue

        if stripped.startswith("### "):
            yield ("h3", stripped[4:].strip())
            i += 1
            continue

        if stripped == "---":
            yield ("hr", None)
            i += 1
            continue

        if stripped.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            yield ("code", "\n".join(buf))
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and lines[i + 1].lstrip().startswith("|---"):
            header = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2  # skip the separator line
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            yield ("table", (header, rows))
            continue

        if stripped.startswith("- ") or stripped.startswith("* "):
            yield ("bullet", stripped[2:])
            i += 1
            continue

        # Paragraph: collect contiguous non-empty, non-special lines
        para = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt
                    or nxt.startswith(("#", "-", "*", "|", "```", "---"))):
                break
            para.append(nxt)
            i += 1
        yield ("para", " ".join(para))


def build():
    pdf = HandbookPDF()
    pdf.cover()

    md_path = os.path.join(OUTPUT_DIR, "HANDBOOK.md")
    table_widths = {
        7: [40, 18, 70, 62],   # tuning table
        2: [55, 135],          # 2-col tables
    }

    for kind, payload in parse_markdown(md_path):
        if kind == "h1":
            pdf.h1(pdf.render_inline(payload))
        elif kind == "h2":
            pdf.h2(pdf.render_inline(payload))
        elif kind == "h3":
            pdf.ln(2)
            pdf._reset_x()
            pdf.set_font("Arial", "B", 10)
            pdf.set_text_color(80, 80, 80)
            pdf.multi_cell(0, 6, pdf.render_inline(payload))
            pdf.ln(1)
        elif kind == "para":
            pdf.body(pdf.render_inline(payload))
        elif kind == "bullet":
            pdf.bullet(pdf.render_inline(payload))
        elif kind == "code":
            pdf.code_block(payload)
        elif kind == "table":
            headers, rows = payload
            n = len(headers)
            widths = table_widths.get(n) or [int(190 / n)] * n
            clean_rows = [[pdf.render_inline(c) for c in r] for r in rows]
            pdf.table(headers, clean_rows, widths)
        elif kind == "hr":
            pdf.ln(2)
            pdf.set_draw_color(200, 200, 200)
            pdf.line(40, pdf.get_y(), 170, pdf.get_y())
            pdf.ln(3)

    out = os.path.join(OUTPUT_DIR, "HANDBOOK.pdf")
    pdf.output(out)
    print(f"Wrote {out}")


if __name__ == "__main__":
    build()
