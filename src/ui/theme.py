"""Bloomberg-style terminal chrome.

Streamlit supplies the data plumbing; this module supplies the look: black
canvas, amber function codes, monospaced figures, dense tables, a scrolling
quote tape and a fixed status bar. Everything is plain CSS so it renders the
same on Windows, macOS and Linux.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

import streamlit as st

WIB = _dt.timezone(_dt.timedelta(hours=7), "WIB")

AMBER = "#ff9e1b"
GREEN = "#00d26a"
RED = "#ff3b30"

CSS = """
<style>
:root {
  --bb-bg: #000000;
  --bb-panel: #0b0c0e;
  --bb-panel-2: #121317;
  --bb-line: #26282c;
  --bb-line-strong: #3c3f45;
  --bb-amber: #ff9e1b;
  --bb-amber-dim: #8a5a10;
  --bb-text: #e8e8e8;
  --bb-muted: #8b8f96;
  --bb-green: #00d26a;
  --bb-red: #ff3b30;
  --bb-blue: #5aa9ff;
  --bb-mono: "JetBrains Mono", "Cascadia Mono", "Consolas", "SF Mono", ui-monospace, monospace;
}

html, body, .stApp, [data-testid="stAppViewContainer"] {
  background: #000000 !important;
  color: var(--bb-text);
  font-family: var(--bb-mono);
}

/* Chrome off: this is an application, not a web page. */
#MainMenu, footer, header[data-testid="stHeader"], [data-testid="stToolbar"],
[data-testid="stDecoration"], [data-testid="stStatusWidget"],
[data-testid="stSidebarCollapsedControl"] {display: none !important;}

.block-container {
  padding: 1.1rem 1.4rem 3.4rem 1.4rem !important;
  max-width: 100% !important;
}

/* ---------- sidebar: function menu ---------- */
[data-testid="stSidebar"] {
  background: var(--bb-panel) !important;
  border-right: 1px solid var(--bb-line);
  min-width: 232px !important;
  max-width: 232px !important;
}
[data-testid="stSidebar"] > div:first-child {padding-top: .6rem;}
[data-testid="stSidebar"] hr {border-color: var(--bb-line); margin: .55rem 0;}
[data-testid="stSidebar"] .terminal-brand {
  font-family: var(--bb-mono); font-size: 15px; font-weight: 700;
  color: var(--bb-amber); letter-spacing: .18em; text-transform: uppercase; line-height: 1.1;
}
[data-testid="stSidebar"] .terminal-sub {
  font-size: 9px; color: var(--bb-muted); letter-spacing: .26em;
  text-transform: uppercase; margin-top: 4px;
}
[data-testid="stSidebar"] [role="radiogroup"] {gap: 0 !important;}
[data-testid="stSidebar"] [role="radiogroup"] > label {
  padding: 5px 6px !important;
  margin: 0 !important;
  border-left: 2px solid transparent;
  border-bottom: 1px solid rgba(255,255,255,.035);
  border-radius: 0 !important;
  cursor: pointer;
  transition: background .08s linear;
}
[data-testid="stSidebar"] [role="radiogroup"] > label:hover {background: #17181c;}
[data-testid="stSidebar"] [role="radiogroup"] > label > div:first-child {display: none;}
[data-testid="stSidebar"] [role="radiogroup"] > label [data-testid="stMarkdownContainer"] p {
  font-family: var(--bb-mono) !important;
  font-size: 11.5px !important;
  letter-spacing: .07em !important;
  text-transform: uppercase;
  color: #b9bcc2 !important;
  line-height: 1.25 !important;
  white-space: pre;
}
[data-testid="stSidebar"] [role="radiogroup"] > label:has(input:checked) {
  background: #1a1408;
  border-left: 2px solid var(--bb-amber);
}
[data-testid="stSidebar"] [role="radiogroup"] > label:has(input:checked) p {color: var(--bb-amber) !important;}

/* ---------- headings ---------- */
h1, h2, h3, h4 {font-family: var(--bb-mono) !important; letter-spacing: .05em;}
h4 {
  text-transform: uppercase; font-size: 11px !important; color: var(--bb-amber) !important;
  border-bottom: 1px solid var(--bb-line); padding-bottom: 4px; margin-top: 1.1rem !important;
}

/* ---------- page header ---------- */
.bb-header {
  display: flex; align-items: baseline; gap: 10px;
  border-bottom: 1px solid var(--bb-amber-dim);
  padding: 2px 0 5px 0; margin: 0 0 2px 0;
}
.bb-code {
  background: var(--bb-amber); color: #000; font-weight: 700; font-size: 11px;
  padding: 2px 7px; letter-spacing: .12em;
}
.bb-title {
  font-size: 15px; font-weight: 700; letter-spacing: .12em;
  text-transform: uppercase; color: #ffffff;
}
.bb-blotter {margin-left: auto; font-size: 10px; color: var(--bb-muted); letter-spacing: .1em;}
.bb-sub {font-size: 10.5px; color: var(--bb-muted); padding: 3px 0 10px 0; letter-spacing: .04em;}

/* ---------- quote tape ---------- */
.bb-tape {
  overflow: hidden; white-space: nowrap; background: #07080a;
  border-top: 1px solid var(--bb-line); border-bottom: 1px solid var(--bb-line);
  margin: 0 0 8px 0; position: relative;
}
.bb-tape-label {
  position: absolute; left: 0; top: 0; bottom: 0; z-index: 2;
  background: var(--bb-amber); color: #000; font-size: 10px; font-weight: 700;
  letter-spacing: .16em; padding: 3px 8px; display: flex; align-items: center;
}
.bb-tape-viewport {overflow: hidden; margin-left: 58px;}
.bb-tape-track {
  display: inline-block; padding: 3px 0; animation: bb-scroll linear infinite;
  will-change: transform;
}
.bb-tape:hover .bb-tape-track {animation-play-state: paused;}
@keyframes bb-scroll {
  from {transform: translateX(0);}
  to   {transform: translateX(-50%);}
}
.bb-tick {
  display: inline-block; padding: 0 14px; font-size: 11px;
  border-right: 1px solid #1b1d21; letter-spacing: .03em;
}
.bb-tick b {color: #fff; font-weight: 700; margin-right: 5px;}
.bb-tick .px {color: #e8e8e8; margin-right: 5px;}
.bb-tick .chg {font-weight: 700;}
.bb-up {color: var(--bb-green);}
.bb-down {color: var(--bb-red);}
.bb-flat {color: var(--bb-muted);}
.bb-tick .late {color: var(--bb-muted); font-size: 9px; margin-left: 4px;}
.bb-tape-empty {padding: 4px 10px; font-size: 10.5px; color: var(--bb-muted);}

/* ---------- live dot ---------- */
.bb-dot {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--bb-green); margin-right: 6px; animation: bb-pulse 1.6s infinite;
}
.bb-dot.closed {background: var(--bb-muted); animation: none;}
.bb-dot.error {background: var(--bb-red); animation: none;}
@keyframes bb-pulse {0%,100% {opacity: 1;} 50% {opacity: .25;}}

/* ---------- status bar ---------- */
.bb-statusbar {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 1000;
  background: #07080a; border-top: 1px solid var(--bb-line);
  font-size: 10px; color: var(--bb-muted); letter-spacing: .06em;
  padding: 3px 12px; display: flex; gap: 18px; text-transform: uppercase;
}
.bb-statusbar b {color: var(--bb-amber); font-weight: 600;}
.bb-statusbar .spacer {margin-left: auto;}

/* ---------- panels ---------- */
.bb-panel {
  border: 1px solid var(--bb-line); background: var(--bb-panel);
  padding: 8px 10px; margin-bottom: 8px;
}
.bb-panel .bb-panel-title {
  font-size: 10px; color: var(--bb-amber); letter-spacing: .14em;
  text-transform: uppercase; border-bottom: 1px solid var(--bb-line);
  padding-bottom: 4px; margin-bottom: 6px;
}
.bb-grid {display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 1px; background: var(--bb-line);}
.bb-cell {background: var(--bb-panel); padding: 6px 9px;}
.bb-cell .k {font-size: 9px; color: var(--bb-muted); text-transform: uppercase; letter-spacing: .12em;}
.bb-cell .v {font-size: 16px; color: #fff; margin-top: 2px;}
.bb-cell .v.up {color: var(--bb-green);}
.bb-cell .v.down {color: var(--bb-red);}
.bb-cell .v.muted {color: var(--bb-muted); font-size: 12px;}

/* ---------- widgets ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
  border-radius: 0 !important;
  border: 1px solid var(--bb-line-strong) !important;
  background: #141518 !important;
  color: var(--bb-amber) !important;
  font-family: var(--bb-mono) !important;
  font-size: 10.5px !important;
  letter-spacing: .1em;
  text-transform: uppercase;
  padding: .3rem .7rem !important;
  min-height: 0 !important;
}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
  background: var(--bb-amber) !important; color: #000 !important; border-color: var(--bb-amber) !important;
}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
  background: var(--bb-amber) !important; color: #000 !important; border-color: var(--bb-amber) !important;
  font-weight: 700 !important;
}
.stButton > button[kind="primary"]:hover {background: #ffb545 !important;}

.stTextInput input, .stNumberInput input, .stDateInput input, .stTextArea textarea {
  background: #08090b !important; color: var(--bb-text) !important;
  border: 1px solid var(--bb-line-strong) !important; border-radius: 0 !important;
  font-family: var(--bb-mono) !important; font-size: 12px !important;
}
.stTextInput input:focus, .stNumberInput input:focus {
  border-color: var(--bb-amber) !important; box-shadow: none !important;
}
[data-baseweb="select"] > div {
  background: #08090b !important; border: 1px solid var(--bb-line-strong) !important;
  border-radius: 0 !important; font-family: var(--bb-mono) !important; font-size: 12px !important;
}
[data-baseweb="popover"] [role="listbox"] {
  background: #0b0c0e !important; border-radius: 0 !important;
  border: 1px solid var(--bb-line-strong) !important;
}
[data-baseweb="tag"] {
  background: #1a1408 !important; color: var(--bb-amber) !important; border-radius: 0 !important;
}
label, .stSelectbox label, .stTextInput label, .stNumberInput label {
  font-size: 10px !important; text-transform: uppercase; letter-spacing: .09em;
  color: var(--bb-muted) !important;
}
[data-testid="stWidgetLabel"] p {font-size: 10px !important; text-transform: uppercase; letter-spacing: .09em; color: var(--bb-muted) !important;}

/* metrics */
[data-testid="stMetric"] {
  border-left: 2px solid var(--bb-amber); background: var(--bb-panel);
  padding: 6px 10px; min-height: 62px;
}
[data-testid="stMetricLabel"] p {
  font-size: 9.5px !important; letter-spacing: .12em; text-transform: uppercase;
  color: var(--bb-muted) !important;
}
[data-testid="stMetricValue"] {font-family: var(--bb-mono); font-size: 20px !important; color: #fff;}
[data-testid="stMetricDelta"] {font-size: 11px !important;}

/* tabs */
.stTabs [data-baseweb="tab-list"] {gap: 0; border-bottom: 1px solid var(--bb-line);}
.stTabs [data-baseweb="tab"] {
  height: 30px; padding: 0 14px; background: transparent;
  font-size: 10px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--bb-muted) !important; border-radius: 0;
}
.stTabs [aria-selected="true"] {
  color: var(--bb-amber) !important; border-bottom: 2px solid var(--bb-amber) !important;
}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] {display: none;}

/* tables, expanders, alerts */
[data-testid="stDataFrame"] {border: 1px solid var(--bb-line); border-radius: 0;}
[data-testid="stDataFrame"] * {font-family: var(--bb-mono) !important;}
[data-testid="stExpander"] {
  border: 1px solid var(--bb-line) !important; border-radius: 0 !important; background: var(--bb-panel);
}
[data-testid="stExpander"] summary {font-size: 11px; letter-spacing: .06em;}
[data-testid="stAlert"] {
  border-radius: 0 !important; border-left: 3px solid var(--bb-line-strong) !important;
  background: var(--bb-panel) !important; font-size: 12px;
}
[data-testid="stAlert"] p {font-size: 12px !important;}
div[data-testid="stAlertContentInfo"] {color: var(--bb-blue);}
div[data-testid="stAlertContentWarning"] {color: var(--bb-amber);}
div[data-testid="stAlertContentPositive"] {color: var(--bb-green);}
div[data-testid="stAlertContentError"] {color: var(--bb-red);}

/* plotly canvas sits on black */
.js-plotly-plot .plotly .modebar {background: transparent !important;}
[data-testid="stPlotlyChart"] {border: 1px solid var(--bb-line);}

/* scrollbars */
::-webkit-scrollbar {width: 8px; height: 8px;}
::-webkit-scrollbar-track {background: #050506;}
::-webkit-scrollbar-thumb {background: #2b2d31; border-radius: 0;}
::-webkit-scrollbar-thumb:hover {background: var(--bb-amber-dim);}

/* checkbox / radio in content */
[data-testid="stCheckbox"] label p {font-size: 11px !important; text-transform: none; letter-spacing: 0;}
</style>
"""


def apply() -> None:
    """Inject the terminal stylesheet. Safe to call on every rerun."""
    st.markdown(CSS, unsafe_allow_html=True)


def page_header(code: str, title: str, subtitle: str = "", blotter: Optional[str] = None) -> None:
    """Bloomberg-style function header: ``MKT  MARKET OVERVIEW``."""
    stamp = blotter if blotter is not None else _dt.datetime.now(WIB).strftime("%d %b %Y  %H:%M:%S WIB")
    st.markdown(
        f"""
<div class="bb-header">
  <span class="bb-code">{code}</span>
  <span class="bb-title">{title}</span>
  <span class="bb-blotter">IDX &middot; {stamp}</span>
</div>
{f'<div class="bb-sub">{subtitle}</div>' if subtitle else '<div style="height:8px"></div>'}
""",
        unsafe_allow_html=True,
    )


def status_bar(provider: str, note: str, db_name: str, version: str) -> None:
    stamp = _dt.datetime.now(WIB).strftime("%H:%M:%S")
    st.markdown(
        f"""
<div class="bb-statusbar">
  <span><b>IDX</b> EQUITY TERMINAL</span>
  <span>FEED: {provider}</span>
  <span>{note}</span>
  <span>DB: {db_name}</span>
  <span class="spacer">{stamp} WIB &nbsp;|&nbsp; v{version}</span>
</div>
""",
        unsafe_allow_html=True,
    )


def change_class(value: Optional[float]) -> str:
    if value is None:
        return "bb-flat"
    if value > 0:
        return "bb-up"
    if value < 0:
        return "bb-down"
    return "bb-flat"


def change_arrow(value: Optional[float]) -> str:
    if value is None:
        return "&middot;"
    if value > 0:
        return "&#9650;"
    if value < 0:
        return "&#9660;"
    return "&#9644;"
