"""Render a standalone, social-ready MCP data-flow graphic.

Produces MCP_FLOW.pdf (a single square page, no report chrome) and rasterises it
to MCP_FLOW.png at high DPI for LinkedIn. Same visual language as the training
deck, but bolder and self-contained.

    python3 generate_mcp_diagram.py
"""

import os

from fpdf import FPDF

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = "/Library/Fonts/Arial Unicode.ttf"
SIDE = 200  # mm, square canvas

BLUE = (30, 80, 160)
GREEN = (34, 139, 94)
ORANGE = (200, 120, 30)
GREY = (105, 110, 120)


class Diagram(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format=(SIDE, SIDE))
        self.add_font("Arial", "", FONT_PATH)
        self.add_font("Arial", "B", FONT_PATH)
        self.set_auto_page_break(False)
        self.set_margins(0, 0, 0)
        self.add_page()

    # primitives ---------------------------------------------------------------
    def _arrowhead(self, x, y, d, s=2.4):
        if d == "right":
            self.line(x, y, x - s, y - s); self.line(x, y, x - s, y + s)
        elif d == "left":
            self.line(x, y, x + s, y - s); self.line(x, y, x + s, y + s)
        elif d == "down":
            self.line(x, y, x - s, y - s); self.line(x, y, x + s, y - s)
        elif d == "up":
            self.line(x, y, x - s, y + s); self.line(x, y, x + s, y + s)

    def harrow(self, x1, x2, y, color, double=True):
        self.set_draw_color(*color); self.set_line_width(0.7)
        self.line(x1, y, x2, y)
        self._arrowhead(x2, y, "right")
        if double:
            self._arrowhead(x1, y, "left")
        self.set_line_width(0.2)

    def varrow(self, x, y1, y2, color, double=True):
        self.set_draw_color(*color); self.set_line_width(0.7)
        self.line(x, y1, x, y2)
        self._arrowhead(x, y2, "down")
        if double:
            self._arrowhead(x, y1, "up")
        self.set_line_width(0.2)

    def box(self, x, y, w, h, title, sub="", fill=(255, 255, 255), border=BLUE,
            tcol=None, scol=(70, 70, 70), ts=12, ss=8.5):
        self.set_fill_color(*fill); self.set_draw_color(*border)
        self.set_line_width(0.7)
        self.rect(x, y, w, h, "DF")
        self.set_line_width(0.2)
        self.set_xy(x, y + 3.2)
        self.set_font("Arial", "B", ts)
        self.set_text_color(*(tcol if tcol else border))
        self.cell(w, 5, title, align="C", ln=1)
        if sub:
            self.set_xy(x + 2, y + 3.2 + ts * 0.42 + 1.5)
            self.set_font("Arial", "", ss)
            self.set_text_color(*scol)
            self.multi_cell(w - 4, 4.2, sub, align="C")

    def badge(self, x, y, n, color, r=3.6):
        self.set_fill_color(*color)
        self.ellipse(x - r, y - r, 2 * r, 2 * r, "F")
        self.set_xy(x - r, y - r + 0.7)
        self.set_font("Arial", "B", 9)
        self.set_text_color(255, 255, 255)
        self.cell(2 * r, 2 * r - 1, str(n), align="C")

    def label(self, x, y, text, color, size=8, w=40):
        self.set_xy(x - w / 2, y)
        self.set_font("Arial", "B", size)
        self.set_text_color(*color)
        self.cell(w, 4, text, align="C")


def build():
    d = Diagram()

    # Header -------------------------------------------------------------------
    d.set_xy(10, 11)
    d.set_font("Arial", "B", 18)
    d.set_text_color(*BLUE)
    d.cell(180, 9, "What happens when I ask: \"What's my P&L?\"", align="C", ln=1)
    d.set_xy(10, 21)
    d.set_font("Arial", "", 11)
    d.set_text_color(110, 110, 110)
    d.cell(180, 6, "A live look at MCP - the Model Context Protocol", align="C", ln=1)
    d.set_draw_color(*BLUE); d.set_line_width(0.5)
    d.line(20, 31, 180, 31); d.set_line_width(0.2)

    # Containers ---------------------------------------------------------------
    mac_x, mac_w = 10, 88
    vm_x, vm_w = 116, 74
    ctop, cbot = 38, 150
    ch = cbot - ctop

    d.set_fill_color(246, 248, 251); d.set_draw_color(180, 190, 205)
    d.set_line_width(0.6); d.rect(mac_x, ctop, mac_w, ch, "DF")
    d.set_fill_color(252, 249, 244); d.set_draw_color(210, 190, 160)
    d.rect(vm_x, ctop, vm_w, ch, "DF"); d.set_line_width(0.2)

    d.set_font("Arial", "B", 10.5)
    d.set_text_color(120, 128, 140)
    d.set_xy(mac_x, ctop + 2.5); d.cell(mac_w, 5, "YOUR MAC", align="C")
    d.set_text_color(175, 135, 85)
    d.set_xy(vm_x, ctop + 2.5); d.cell(vm_w, 5, "LIGHTSAIL VM  -  cloud", align="C")

    cx = mac_x + mac_w / 2          # 54
    vcx = vm_x + vm_w / 2           # 153

    # YOU
    d.box(mac_x + 6, ctop + 9, mac_w - 12, 16, "YOU",
          "\"What's my total P&L?\"  (plain English)",
          border=GREY, tcol=(70, 70, 70), ts=11)
    # HOST + CLIENT
    host_y, host_h = ctop + 30, 46
    d.box(mac_x + 5, host_y, mac_w - 10, host_h, "HOST  -  Claude Desktop / Code",
          "the app you chat with", fill=(232, 240, 252), border=BLUE, ts=11)
    d.box(mac_x + 11, host_y + 19, mac_w - 22, 23, "MCP CLIENT",
          "finds & calls the right tool", fill=BLUE, border=BLUE,
          tcol=(255, 255, 255), scol=(220, 232, 255), ts=11.5)
    # SERVER
    srv_y, srv_h = ctop + 82, 26
    d.box(mac_x + 5, srv_y, mac_w - 10, srv_h, "MCP SERVER",
          "mcp_trades_server.py  -  you control it", fill=GREEN, border=GREEN,
          tcol=(255, 255, 255), scol=(222, 245, 233), ts=11.5)
    # DB
    db_y, db_h = srv_y, 26
    # VM context box, tied to the db below with a dashed "same machine" guide
    d.box(vm_x + 6, ctop + 13, vm_w - 12, 19, "Where the ORB bot runs",
          "logs every trade to trades.db", fill=(255, 250, 243),
          border=(214, 196, 168), tcol=(150, 120, 70), scol=(120, 110, 95),
          ts=10.5, ss=8.5)
    d.set_draw_color(214, 196, 168)
    d.set_dash_pattern(dash=1.5, gap=1.5)
    d.line(vcx, ctop + 32, vcx, db_y)
    d.set_dash_pattern()
    d.box(vm_x + 6, db_y, vm_w - 12, db_h, "trades.db  (SQLite)",
          "opened READ-ONLY", fill=(255, 241, 222), border=ORANGE, ts=11.5)

    # Arrows -------------------------------------------------------------------
    # 1: your question enters the host (which holds the client)
    d.varrow(cx, ctop + 25, host_y, GREY, double=False)
    d.badge(cx + 7, (ctop + 25 + host_y) / 2, 1, GREY)

    # 2: client <-> server, over stdio (same machine)
    d.varrow(cx, host_y + host_h - 4, srv_y, (60, 60, 60))
    midv = (host_y + host_h - 4 + srv_y) / 2
    d.badge(cx + 7, midv, 2, (60, 60, 60))
    d.label(cx - 22, midv - 4.5, "stdio", (70, 70, 70), size=8.5, w=22)

    # 3: server -> database, over SSH (across the network)
    arr_y = srv_y + srv_h / 2
    d.harrow(mac_x + mac_w - 5, vm_x + 6, arr_y, GREEN)
    midh = (mac_x + mac_w - 5 + vm_x + 6) / 2
    d.badge(midh, arr_y - 8, 3, GREEN)
    d.label(midh, arr_y - 15, "SSH", GREEN, size=9, w=24)
    d.label(midh, arr_y + 4.5, "port 22", GREEN, size=8, w=24)

    # 4: read-only read of the live table
    d.badge(vm_x + vm_w - 10, db_y - 3, 4, ORANGE)

    # Takeaway + legend --------------------------------------------------------
    d.set_xy(10, 156)
    d.set_font("Arial", "B", 11)
    d.set_text_color(45, 45, 45)
    d.cell(180, 6, "The AI asks. My read-only server answers from the live database.", align="C", ln=1)
    d.set_x(10)
    d.set_font("Arial", "", 10.5)
    d.set_text_color(90, 90, 90)
    d.cell(180, 6, "I never log into the server - and it can't change a thing.", align="C", ln=1)

    legend = [(GREY, "You / Host"), (BLUE, "MCP Client"),
              (GREEN, "MCP Server"), (ORANGE, "Database")]
    d.set_font("Arial", "", 9.5)
    item_w = 44
    sx = (SIDE - item_w * len(legend)) / 2 + 4
    ly = 178
    for color, text in legend:
        d.set_fill_color(*color); d.rect(sx, ly, 5, 5, "F")
        d.set_xy(sx + 6.5, ly - 0.6)
        d.set_text_color(70, 70, 70)
        d.cell(item_w - 10, 6, text)
        sx += item_w

    out_pdf = os.path.join(OUTPUT_DIR, "MCP_FLOW.pdf")
    d.output(out_pdf)
    print(f"Wrote {out_pdf}")
    return out_pdf


if __name__ == "__main__":
    build()
