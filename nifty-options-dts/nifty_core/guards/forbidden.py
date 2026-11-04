"""Definitions and scanners used by the safety tests to prove nothing here can trade live.

This file deliberately contains the forbidden words, so the scanners skip it.
"""
import ast
import os
import re

# A data-provider method whose name contains one of these words is order-capable.
FORBIDDEN_METHOD_WORDS = (
    "place", "order", "submit", "execute", "buy", "sell", "modify", "cancel",
    "squareoff", "square_off", "exercise", "trade", "send", "transmit",
)

# Source text patterns that indicate an order interface. (name, compiled regex)
FORBIDDEN_SOURCE_PATTERNS = (
    ("place_order call", re.compile(r"place_?(stock_|option_|crypto_|gtt_)?orders?\b", re.I)),
    ("modify/cancel order", re.compile(r"\b(modify|cancel|replace)_?orders?\b", re.I)),
    ("close_all_positions", re.compile(r"close_?all_?positions", re.I)),
    ("exercise option", re.compile(r"\bexercise_?options?", re.I)),
    ("broker order url", re.compile(r"https?://[^\s'\"]*/(orders?|place|trade)\b", re.I)),
    ("known broker order sdk", re.compile(r"\b(kite\.place|kiteconnect|smartapi|upstox_client|fyers_apiv|dhanhq|alpaca[_-]?(trade|py|mcp))", re.I)),
    ("live flag enabled", re.compile(r"\b(live|paper)[\"']?\s*[:=]\s*(?:false|\"false\"|'false')", re.I)),
)

# Modules that can reach a network or spawn programs. None are allowed in nifty_core in V1.
FORBIDDEN_IMPORTS_IN_CORE = (
    "socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp",
    "websocket", "websockets", "ftplib", "smtplib", "telnetlib", "xmlrpc",
    "subprocess", "asyncio", "ctypes",
)

# Looks like a hard-coded credential.
SECRET_PATTERN = re.compile(
    r"(api[_-]?key|api[_-]?secret|secret[_-]?key|access[_-]?token|auth[_-]?token|password)"
    r"\s*[:=]\s*['\"][A-Za-z0-9_\-/+=]{16,}['\"]", re.I)


def method_name_is_forbidden(name):
    low = name.lower()
    return any(word in low for word in FORBIDDEN_METHOD_WORDS)


def scan_source(text):
    """Return a list of (rule, snippet) findings for forbidden order patterns and secrets."""
    findings = []
    for name, rx in FORBIDDEN_SOURCE_PATTERNS:
        for m in rx.finditer(text):
            findings.append((name, m.group(0)))
    for m in SECRET_PATTERN.finditer(text):
        findings.append(("hard-coded credential", m.group(0)[:30] + "..."))
    return findings


def imported_modules(source):
    """Top-level module names imported by a Python source string."""
    mods = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def forbidden_imports(source):
    return sorted(imported_modules(source) & set(FORBIDDEN_IMPORTS_IN_CORE))


def python_files(root, skip_dirs=(".git", "__pycache__")):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(base, name)
