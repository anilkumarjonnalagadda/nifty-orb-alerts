"""Render MCP_EXPLAINED.pdf — a training deck on the Model Context Protocol.

Uses this repo's own `trades` MCP server as the live example. Built with the
same FPDF style as generate_handbook_pdf.py. Regenerate with:

    python3 generate_mcp_pdf.py
"""

import os

from fpdf import FPDF

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = "/Library/Fonts/Arial Unicode.ttf"

# Palette
BLUE = (30, 80, 160)
GREEN = (34, 139, 94)
ORANGE = (200, 120, 30)
GREY = (90, 90, 90)
LIGHT = (245, 247, 250)


class MCPDeck(FPDF):
    def __init__(self):
        super().__init__()
        self.add_font("Arial", "", FONT_PATH)
        self.add_font("Arial", "B", FONT_PATH)
        self.set_auto_page_break(auto=True, margin=18)
        self.add_page()

    # --- chrome ---------------------------------------------------------------
    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("Arial", "B", 9)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, "Understanding MCP — Model Context Protocol", align="R")
        self.ln(2)
        self.set_draw_color(210, 210, 210)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self):
        if self.page_no() == 1:
            return
        self.set_y(-14)
        self.set_font("Arial", "", 8)
        self.set_text_color(160, 160, 160)
        self.cell(0, 8, f"Page {self.page_no()}  |  MCP Training  |  live example: the ORB trades server", align="C")

    def cover(self):
        self.set_fill_color(*BLUE)
        self.rect(0, 0, 210, 297, "F")
        self.set_y(95)
        self.set_font("Arial", "B", 30)
        self.set_text_color(255, 255, 255)
        self.cell(0, 16, "Understanding MCP", align="C", ln=1)
        self.set_font("Arial", "B", 16)
        self.set_text_color(200, 220, 255)
        self.cell(0, 10, "The Model Context Protocol, end to end", align="C", ln=1)
        self.ln(10)
        self.set_font("Arial", "", 12)
        self.set_text_color(225, 235, 255)
        self.cell(0, 8, "MCP Client vs MCP Server  -  and how data flows", align="C", ln=1)
        self.cell(0, 8, "A live example: querying a trading bot on a remote VM", align="C", ln=1)
        self.ln(30)
        self.set_font("Arial", "", 11)
        self.set_text_color(180, 205, 245)
        self.cell(0, 7, "Product Training Material", align="C", ln=1)
        self.add_page()

    # --- text helpers ---------------------------------------------------------
    def h1(self, text):
        self.ln(2)
        self.set_x(self.l_margin)
        self.set_font("Arial", "B", 17)
        self.set_text_color(*BLUE)
        self.multi_cell(0, 9, text)
        self.set_draw_color(*BLUE)
        self.set_line_width(0.5)
        self.line(10, self.get_y(), 200, self.get_y())
        self.set_line_width(0.2)
        self.ln(3)

    def h2(self, text, color=(40, 40, 40)):
        self.ln(2)
        self.set_x(self.l_margin)
        self.set_font("Arial", "B", 12.5)
        self.set_text_color(*color)
        self.multi_cell(0, 7, text)
        self.ln(1)

    def body(self, text):
        self.set_x(self.l_margin)
        self.set_font("Arial", "", 10.5)
        self.set_text_color(55, 55, 55)
        self.multi_cell(0, 6, text)
        self.ln(1.5)

    def bullet(self, text, bold_lead=None):
        self.set_x(15)
        self.set_font("Arial", "B", 10.5)
        self.set_text_color(*BLUE)
        self.cell(5, 6, "-", ln=0)
        if bold_lead:
            self.set_text_color(40, 40, 40)
            self.set_font("Arial", "B", 10.5)
            self.cell(self.get_string_width(bold_lead) + 1, 6, bold_lead, ln=0)
        self.set_font("Arial", "", 10.5)
        self.set_text_color(55, 55, 55)
        self.multi_cell(0, 6, text)
        self.set_x(self.l_margin)

    def callout(self, title, text, color=BLUE):
        self.ln(1)
        self.set_x(self.l_margin)
        x, y = 10, self.get_y()
        # measure
        self.set_font("Arial", "", 10)
        # estimate height
        self.set_xy(x + 5, y + 6)
        self.set_fill_color(*LIGHT)
        self.set_draw_color(*color)
        # draw a placeholder; compute height by rendering text in a temp pass is complex,
        # so use a fixed-ish height based on text length
        approx_lines = max(1, int(len(text) / 95) + 1)
        h = 10 + approx_lines * 5.5
        self.set_line_width(0.4)
        self.rect(x, y, 190, h, "DF")
        self.set_line_width(0.2)
        # accent bar
        self.set_fill_color(*color)
        self.rect(x, y, 2.5, h, "F")
        self.set_xy(x + 6, y + 3)
        self.set_font("Arial", "B", 10.5)
        self.set_text_color(*color)
        self.cell(0, 5, title, ln=1)
        self.set_xy(x + 6, y + 9)
        self.set_font("Arial", "", 10)
        self.set_text_color(55, 55, 55)
        self.multi_cell(178, 5.5, text)
        self.set_y(y + h + 3)
        self.set_x(self.l_margin)

    def code_block(self, text):
        self.ln(1)
        self.set_x(self.l_margin)
        self.set_fill_color(238, 242, 248)
        self.set_draw_color(200, 210, 225)
        lines = text.strip("\n").split("\n") or [""]
        line_h = 5
        total_h = len(lines) * line_h + 6
        y = self.get_y()
        if y + total_h > 270:
            self.add_page()
            y = self.get_y()
        self.rect(10, y, 190, total_h, "DF")
        self.set_xy(13, y + 3)
        self.set_font("Courier", "", 9)
        self.set_text_color(30, 40, 90)
        for line in lines:
            self.set_x(13)
            self.cell(0, line_h, line, ln=1)
        self.ln(2)
        self.set_x(self.l_margin)

    def table(self, headers, rows, widths):
        self.ln(1)
        self.set_fill_color(*BLUE)
        self.set_text_color(255, 255, 255)
        self.set_font("Arial", "B", 9.5)
        for i, htext in enumerate(headers):
            self.cell(widths[i], 8, f"  {htext}", border=0, fill=True)
        self.ln()
        for ri, row in enumerate(rows):
            fill = ri % 2 == 0
            self.set_fill_color(*( (240, 244, 251) if fill else (255, 255, 255)))
            self.set_text_color(45, 45, 45)
            self.set_font("Arial", "", 9.5)
            cell_lines = [self._wrap(str(c), widths[i] - 4) for i, c in enumerate(row)]
            max_lines = max(len(cl) for cl in cell_lines)
            row_h = 5.5 * max_lines + 2
            x0, y0 = self.get_x(), self.get_y()
            if y0 + row_h > 275:
                self.add_page()
                y0 = self.get_y()
            for i, cl in enumerate(cell_lines):
                self.set_xy(x0, y0)
                self.cell(widths[i], row_h, "", border=0, fill=True)
                self.set_xy(x0 + 2, y0 + 1)
                self.multi_cell(widths[i] - 4, 5.5, str(row[i]))
                x0 += widths[i]
            self.set_xy(10, y0 + row_h)
        self.ln(2)

    def _wrap(self, text, width_mm):
        chars = max(1, int(width_mm / 1.7))
        words, lines, cur = text.split(), [], ""
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

    # --- diagram primitives ---------------------------------------------------
    def _arrowhead(self, x, y, direction, s=2.0):
        if direction == "right":
            self.line(x, y, x - s, y - s); self.line(x, y, x - s, y + s)
        elif direction == "left":
            self.line(x, y, x + s, y - s); self.line(x, y, x + s, y + s)
        elif direction == "down":
            self.line(x, y, x - s, y - s); self.line(x, y, x + s, y - s)
        elif direction == "up":
            self.line(x, y, x - s, y + s); self.line(x, y, x + s, y + s)

    def harrow(self, x1, x2, y, color=GREY, double=True):
        self.set_draw_color(*color)
        self.set_line_width(0.5)
        self.line(x1, y, x2, y)
        self._arrowhead(x2, y, "right")
        if double:
            self._arrowhead(x1, y, "left")
        self.set_line_width(0.2)

    def varrow(self, x, y1, y2, color=GREY, double=True):
        self.set_draw_color(*color)
        self.set_line_width(0.5)
        self.line(x, y1, x, y2)
        self._arrowhead(x, y2, "down")
        if double:
            self._arrowhead(x, y1, "up")
        self.set_line_width(0.2)

    def dbox(self, x, y, w, h, title, subtitle="", fill=(255, 255, 255),
             border=BLUE, title_color=None, sub_color=(70, 70, 70), solid=False):
        self.set_fill_color(*fill)
        self.set_draw_color(*border)
        self.set_line_width(0.5)
        self.rect(x, y, w, h, "DF")
        self.set_line_width(0.2)
        tc = title_color if title_color else border
        self.set_xy(x, y + 3)
        self.set_font("Arial", "B", 10)
        self.set_text_color(*tc)
        self.cell(w, 5, title, align="C", ln=1)
        if subtitle:
            self.set_xy(x + 2, y + 9)
            self.set_font("Arial", "", 8)
            self.set_text_color(*sub_color)
            self.multi_cell(w - 4, 4.2, subtitle, align="C")

    def step_badge(self, x, y, n, color=BLUE):
        r = 3.2
        self.set_fill_color(*color)
        self.set_draw_color(255, 255, 255)
        self.ellipse(x - r, y - r, 2 * r, 2 * r, "F")
        self.set_xy(x - r, y - r + 0.6)
        self.set_font("Arial", "B", 8)
        self.set_text_color(255, 255, 255)
        self.cell(2 * r, 2 * r - 1, str(n), align="C")

    def small_label(self, x, y, text, color=GREY, size=7.5, align="C", w=40):
        self.set_xy(x - w / 2, y)
        self.set_font("Arial", "", size)
        self.set_text_color(*color)
        self.cell(w, 4, text, align=align)


def draw_flow_diagram(pdf):
    """The centerpiece: how a plain-English question reaches trades.db on the VM."""
    # Containers
    mac_x, mac_w = 10, 92
    vm_x, vm_w = 118, 82
    top, bottom = 52, 180
    cont_h = bottom - top

    # Mac container
    pdf.set_draw_color(180, 190, 205)
    pdf.set_fill_color(247, 249, 252)
    pdf.set_line_width(0.5)
    pdf.rect(mac_x, top, mac_w, cont_h, "DF")
    # VM container
    pdf.set_fill_color(252, 249, 244)
    pdf.set_draw_color(210, 190, 160)
    pdf.rect(vm_x, top, vm_w, cont_h, "DF")
    pdf.set_line_width(0.2)

    # Container labels
    pdf.set_font("Arial", "B", 10)
    pdf.set_text_color(110, 120, 135)
    pdf.set_xy(mac_x, top + 2)
    pdf.cell(mac_w, 5, "YOUR MAC", align="C")
    pdf.set_text_color(170, 130, 80)
    pdf.set_xy(vm_x, top + 2)
    pdf.cell(vm_w, 5, "LIGHTSAIL VM  (Ubuntu, in the cloud)", align="C")

    cx_mac = mac_x + mac_w / 2  # 56

    # YOU box
    pdf.dbox(mac_x + 8, top + 9, mac_w - 16, 17, "YOU",
             "\"What's my total paper P&L?\"  (plain English)",
             fill=(255, 255, 255), border=GREY, title_color=(60, 60, 60))

    # HOST box (Claude Code) containing the CLIENT
    host_y, host_h = top + 33, 50
    pdf.dbox(mac_x + 6, host_y, mac_w - 12, host_h,
             "HOST  -  Claude Code", "The AI app you chat with. Runs the model.",
             fill=(232, 240, 252), border=BLUE)
    # CLIENT box (inside host), emphasized solid blue
    pdf.dbox(mac_x + 12, host_y + 22, mac_w - 24, 22, "MCP CLIENT",
             "Opens & owns the connection. Discovers and calls tools.",
             fill=BLUE, border=BLUE, title_color=(255, 255, 255), sub_color=(225, 235, 255))

    # SERVER box, emphasized solid green
    srv_y, srv_h = top + 96, 26
    pdf.dbox(mac_x + 6, srv_y, mac_w - 12, srv_h, "MCP SERVER",
             "mcp_trades_server.py  -  exposes tools:\nrun_query, get_schema, recent_trades",
             fill=GREEN, border=GREEN, title_color=(255, 255, 255), sub_color=(225, 245, 235))

    # DB box on VM
    db_y, db_h = srv_y, 26
    pdf.dbox(vm_x + 8, db_y, vm_w - 16, db_h, "trades.db  (SQLite)",
             "table: trades\nopened with  sqlite3 -readonly",
             fill=(255, 241, 222), border=ORANGE, title_color=ORANGE)

    # Arrows -------------------------------------------------------------------
    # 1: YOU -> HOST (down)
    pdf.varrow(cx_mac, top + 26, host_y, color=GREY, double=False)
    pdf.step_badge(cx_mac + 6, (top + 26 + host_y) / 2, 1, GREY)

    # 2: model -> client (inside host, short down)
    pdf.varrow(cx_mac, host_y + 13, host_y + 22, color=BLUE, double=False)
    pdf.step_badge(cx_mac + 6, host_y + 17.5, 2, BLUE)

    # 3: CLIENT <-> SERVER (down, stdio)
    pdf.varrow(cx_mac, host_y + 44, srv_y, color=(60, 60, 60))
    pdf.step_badge(cx_mac + 6, (host_y + 44 + srv_y) / 2, 3, (60, 60, 60))
    pdf.small_label(cx_mac - 24, (host_y + 44 + srv_y) / 2 - 2, "stdio", color=(80, 80, 80), w=20)
    pdf.small_label(cx_mac - 24, (host_y + 44 + srv_y) / 2 + 1.5, "JSON-RPC", color=(80, 80, 80), w=20)

    # 4: SERVER <-> VM db (right, SSH across the gap)
    arr_y = srv_y + srv_h / 2
    pdf.harrow(mac_x + mac_w - 6, vm_x + 8, arr_y, color=GREEN)
    midx = (mac_x + mac_w - 6 + vm_x + 8) / 2
    pdf.step_badge(midx, arr_y - 7, 4, GREEN)
    pdf.small_label(midx, arr_y - 13, "SSH", color=GREEN, w=24, size=8)
    pdf.small_label(midx, arr_y + 4, "port 22", color=GREEN, w=24)

    # 5: db read (label inside VM)
    pdf.step_badge(vm_x + vm_w - 12, db_y - 4, 5, ORANGE)

    # Caption under the diagram
    pdf.set_xy(10, bottom + 8)
    pdf.set_font("Arial", "", 9.5)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(
        190, 5.5,
        "Steps 1-5 carry your request OUT to the database; the result retraces the "
        "same path BACK to you (step 6). The database is opened read-only, so the "
        "demo can never change real trade data.",
        align="C",
    )

    # Color legend
    ly = pdf.get_y() + 5
    legend = [(GREY, "You / Host"), (BLUE, "MCP Client"),
              (GREEN, "MCP Server"), (ORANGE, "Database")]
    pdf.set_font("Arial", "", 9)
    item_w = 44
    total = item_w * len(legend)
    sx = (210 - total) / 2 + 6
    for color, label in legend:
        pdf.set_fill_color(*color)
        pdf.set_draw_color(*color)
        pdf.rect(sx, ly, 4.5, 4.5, "F")
        pdf.set_xy(sx + 6, ly - 0.5)
        pdf.set_text_color(70, 70, 70)
        pdf.cell(item_w - 10, 5.5, label)
        sx += item_w


def build():
    pdf = MCPDeck()
    pdf.cover()

    # ---- Page: What is MCP ----
    pdf.h1("1.  What is MCP?")
    pdf.body(
        "MCP (Model Context Protocol) is an open standard that lets an AI assistant "
        "talk to outside tools and data in one consistent way. Think of it as a "
        "universal adapter - like USB-C for AI. Instead of hand-coding a custom "
        "integration for every app, every database, and every model, each side speaks "
        "the same protocol and they just plug together."
    )
    pdf.h2("The problem it solves", color=BLUE)
    pdf.body(
        "Without a standard, connecting M AI apps to N data sources means building "
        "M x N one-off integrations. MCP turns that into M + N: each AI app ships a "
        "client once, each data source ships a server once, and any client can talk to "
        "any server."
    )
    pdf.callout(
        "In one sentence",
        "MCP standardises HOW an AI app asks for tools and data, so the same little "
        "'server' you write can be reused by any MCP-aware app - and your app can use "
        "any MCP server without custom glue.",
        color=BLUE,
    )
    pdf.h2("The three roles you'll hear about", color=BLUE)
    pdf.bullet("the AI application you interact with (e.g. Claude Code). It hosts the model and one or more clients.", bold_lead="Host: ")
    pdf.bullet("a connector living inside the host. It opens a connection to ONE server, discovers its tools, and calls them on the model's behalf.", bold_lead="Client: ")
    pdf.bullet("a small program that exposes tools/data over the protocol. It does the actual work when a tool is called.", bold_lead="Server: ")

    # ---- Page: Client vs Server ----
    pdf.add_page()
    pdf.h1("2.  MCP Client vs MCP Server")
    pdf.body(
        "This is the distinction to anchor on. The client is the CALLER; the server is "
        "the DOER. They have a strict 1-to-1 connection and talk in structured "
        "messages (JSON-RPC), never free text."
    )
    pdf.table(
        ["", "MCP CLIENT", "MCP SERVER"],
        [
            ["Lives in", "The AI app (the host) - e.g. Claude Code", "A standalone program you run - e.g. mcp_trades_server.py"],
            ["Job", "Discover tools, decide when to call them, send the call", "Receive the call, do the work, return a result"],
            ["Knows about", "Many possible servers", "Only its own tools + data"],
            ["Our setup", "Built into Claude Code on your Mac", "The 'trades' server that reads trades.db"],
            ["Initiates?", "Yes - the client always starts the conversation", "No - it waits and responds"],
        ],
        widths=[28, 81, 81],
    )
    pdf.h2("What a server actually exposes", color=GREEN)
    pdf.bullet("functions the model can call (with typed inputs). This is what we use.", bold_lead="Tools: ")
    pdf.bullet("read-only data the model can pull in for context (files, records).", bold_lead="Resources: ")
    pdf.bullet("reusable prompt templates the server offers. (Not used here.)", bold_lead="Prompts: ")
    pdf.body("Our server exposes three TOOLS:")
    pdf.code_block(
        "run_query(sql)              -> run a read-only SELECT, return rows\n"
        "get_schema()               -> list the columns of the trades table\n"
        "recent_trades(limit, mode) -> shortcut for the latest trades"
    )
    pdf.h2("How they connect: the transport", color=GREEN)
    pdf.bullet("client and server on the same machine talk over standard input/output. Simplest, nothing exposed to the network. This is what we use.", bold_lead="stdio (local): ")
    pdf.bullet("the server runs as a web service the client reaches over the network. Needed when the server lives elsewhere.", bold_lead="HTTP / SSE (remote): ")

    # ---- Page: the diagram ----
    pdf.add_page()
    pdf.h1("3.  How the data flows  (our live example)")
    pdf.body(
        "You ask a question in plain English; the answer comes from a SQLite database "
        "on a remote Ubuntu VM. Follow the numbered steps:"
    )
    draw_flow_diagram(pdf)

    # ---- Page: the round trip in words ----
    pdf.add_page()
    pdf.h1("4.  Walking the round trip")
    pdf.body("Each numbered step on the diagram, in order:")
    steps = [
        ("1", "You ask, in plain English", GREY,
         "In Claude Code you type: \"What's my total paper P&L?\" No SQL, no commands."),
        ("2", "The model decides it needs data", BLUE,
         "The model (inside the Host) realises it should call a tool, and hands the request to the MCP Client."),
        ("3", "Client calls the server over stdio", (60, 60, 60),
         "The Client sends a structured 'tools/call' message (JSON-RPC) to the MCP Server over standard input/output. e.g. run_query('SELECT SUM(pnl) ...')."),
        ("4", "Server reaches the VM over SSH", GREEN,
         "The Server first checks the SQL is read-only, then opens an SSH connection to the VM (orb-vm) and runs sqlite3 -readonly against trades.db."),
        ("5", "SQLite reads the database", ORANGE,
         "On the VM, sqlite3 reads the 'trades' table read-only and returns the matching rows as JSON. The live data is never modified."),
        ("6", "The answer flows back", BLUE,
         "Rows travel back: VM -> Server -> (stdio) -> Client -> model. The model turns the rows into a plain-English answer: \"Your total paper P&L is -Rs.10,699 across 7 trades.\""),
    ]
    for n, title, color, text in steps:
        y = pdf.get_y()
        pdf.step_badge(15, y + 3, n, color)
        pdf.set_xy(21, y)
        pdf.set_font("Arial", "B", 11)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, 6, title)
        pdf.set_x(21)
        pdf.set_font("Arial", "", 10)
        pdf.set_text_color(55, 55, 55)
        pdf.multi_cell(178, 5.5, text)
        pdf.ln(3)

    pdf.callout(
        "Why route through SSH instead of opening the database to the internet?",
        "The VM never exposes a database port. The server reaches it over the same "
        "secure SSH channel you already trust, and opens the file read-only - so a "
        "training demo can never corrupt real trade data.",
        color=GREEN,
    )

    # ---- Page: key terms ----
    pdf.add_page()
    pdf.h1("5.  Key terms for the session")
    pdf.table(
        ["Term", "Plain-English meaning"],
        [
            ["MCP", "Open standard for connecting AI assistants to tools & data."],
            ["Host", "The AI app you use (Claude Code). Holds the model + clients."],
            ["Client", "The connector inside the host that calls one server."],
            ["Server", "A small program that exposes tools and does the work."],
            ["Tool", "A callable function the model can invoke (e.g. run_query)."],
            ["Resource", "Read-only data a server offers for context."],
            ["Transport", "How client & server talk: stdio (local) or HTTP (remote)."],
            ["JSON-RPC", "The structured message format they exchange."],
            ["stdio", "Talking over standard input/output - same-machine, no network."],
        ],
        widths=[40, 150],
    )
    pdf.callout(
        "The one-line takeaway",
        "An MCP CLIENT (inside your AI app) calls an MCP SERVER (a small program you "
        "control); the server does the real work - here, securely reading a database "
        "on a remote VM - and hands the result back to the model.",
        color=BLUE,
    )

    out = os.path.join(OUTPUT_DIR, "MCP_EXPLAINED.pdf")
    pdf.output(out)
    print(f"Wrote {out}")


if __name__ == "__main__":
    build()
