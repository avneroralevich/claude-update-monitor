#!/usr/bin/env python3
"""
Anthropic Claude Update Monitor
Checks for new updates across Claude Code, Claude.ai, Claude API, and Anthropic News.
Sends Telegram notifications with English content + Hebrew translation.
"""

import json
import os
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")   # auto-set by Actions
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
LAST_SEEN_FILE   = "last_seen.json"

WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClaudeUpdateMonitor/1.0)"
}

# ── State helpers ───────────────────────────────────────────────────────────────
def load_last_seen() -> dict:
    if os.path.exists(LAST_SEEN_FILE):
        with open(LAST_SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_last_seen(data: dict):
    with open(LAST_SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── Telegram ────────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] credentials not configured — printing message instead:\n")
        print(text)
        return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": False,
    }, timeout=15)
    if resp.ok:
        print("[Telegram] message sent.")
    else:
        print(f"[Telegram] error {resp.status_code}: {resp.text}")

# ── Hebrew explanation via Gemini ─────────────────────────────────────────────
def explain_hebrew(text: str) -> str:
    """Generate a simple Hebrew explanation using Google Gemini API (free tier)."""
    if not GEMINI_API_KEY:
        print("[Gemini] API key not configured, skipping Hebrew explanation")
        return ""
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent?key={GEMINI_API_KEY}"
        prompt = (
            "אתה עוזר טכני שמסביר עדכוני תוכנה בעברית פשוטה.\n"
            "קיבלת את העדכון הבא באנגלית. תן הסבר קצר (2-3 משפטים) בעברית פשוטה "
            "שגם מי שלא מתכנת יבין. אל תתרגם מילה במילה - תסביר את המשמעות.\n\n"
            f"העדכון:\n{text[:800]}"
        )
        resp = requests.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}]
        }, timeout=20)
        if resp.ok:
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        else:
            print(f"[Gemini] error {resp.status_code}: {resp.text[:200]}")
            return ""
    except Exception as exc:
        print(f"[Gemini] {exc}")
        return ""

# ── Message formatter ───────────────────────────────────────────────────────────
def build_message(emoji: str, source: str, items: list[dict], fallback_url: str) -> str:
    date_str = datetime.now().strftime("%B %d, %Y")
    lines = [
        f"{emoji} <b>New Update — {source}</b>",
        f"📅 {date_str}",
        "─────────────────────",
    ]

    for item in items[:3]:
        version = item.get("version", "")
        title   = item.get("title",   "")
        body    = item.get("body",    "")
        url     = item.get("url",     fallback_url)

        if version:
            lines.append(f"\n<b>{version}</b>")
        if title and title != version:
            lines.append(f"<b>{title}</b>")

        if body:
            short = body[:400] + "…" if len(body) > 400 else body
            lines.append(short)

        # Hebrew translation of the most descriptive text
        source_text = title or body
        if source_text:
            heb = explain_hebrew(source_text)
            if heb:
                lines.append(f"\n🇮🇱 <i>{heb}</i>")

        lines.append(f'\n🔗 <a href="{url}">Full release notes →</a>')

    return "\n".join(lines)

# ── Source 1: Claude Code (GitHub Releases API) ─────────────────────────────────
def check_claude_code(last_seen: dict) -> tuple[list | None, str | None]:
    print("[Claude Code] checking GitHub releases …")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    resp = requests.get(
        "https://api.github.com/repos/anthropics/claude-code/releases",
        headers=headers, timeout=15
    )
    if not resp.ok:
        print(f"  GitHub API error {resp.status_code}")
        return None, last_seen.get("claude_code")

    releases = resp.json()
    if not releases:
        return None, last_seen.get("claude_code")

    latest_tag = releases[0]["tag_name"]
    last_tag   = last_seen.get("claude_code")

    if latest_tag == last_tag:
        print(f"  No new releases (latest: {latest_tag})")
        return None, latest_tag

    new_releases = []
    for r in releases:
        if r["tag_name"] == last_tag:
            break
        new_releases.append({
            "version": r["tag_name"],
            "title":   r["name"] or r["tag_name"],
            "body":    (r["body"] or "").strip(),
            "url":     r["html_url"],
        })

    print(f"  Found {len(new_releases)} new release(s)")
    return new_releases, latest_tag

# ── Source 2: Anthropic News ────────────────────────────────────────────────────
def check_anthropic_news(last_seen: dict) -> tuple[list | None, str | None]:
    print("[Anthropic News] checking …")
    resp = requests.get("https://www.anthropic.com/news", headers=WEB_HEADERS, timeout=15)
    if not resp.ok:
        print(f"  Error {resp.status_code}")
        return None, last_seen.get("anthropic_news")

    soup = BeautifulSoup(resp.text, "lxml")
    seen_urls: set[str] = set()
    articles: list[dict] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if "/news/" not in href or href.rstrip("/") == "/news":
            continue
        full_url = ("https://www.anthropic.com" + href) if href.startswith("/") else href
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        heading = a.find(["h2", "h3", "h4"])
        title   = (heading or a).get_text(strip=True)
        if len(title) > 10:
            articles.append({"title": title, "url": full_url, "body": ""})

    if not articles:
        print("  Could not parse articles")
        return None, last_seen.get("anthropic_news")

    latest_url = articles[0]["url"]
    last_url   = last_seen.get("anthropic_news")

    if latest_url == last_url:
        print("  No new articles")
        return None, latest_url

    new_articles = []
    for a in articles:
        if a["url"] == last_url:
            break
        new_articles.append(a)

    print(f"  Found {len(new_articles)} new article(s)")
    return new_articles, latest_url

# ── Source 3 & 4: Generic page-change detector (hash of top content) ────────────
def check_page(key: str, url: str, label: str, last_seen: dict) -> tuple[list | None, str | None]:
    print(f"[{label}] checking …")
    resp = requests.get(url, headers=WEB_HEADERS, timeout=15)
    if not resp.ok:
        print(f"  Error {resp.status_code}")
        return None, last_seen.get(key)

    soup = BeautifulSoup(resp.text, "lxml")
    main = soup.find("main") or soup.find("article") or soup.body
    if not main:
        return None, last_seen.get(key)

    # Hash the first 3000 chars of text — new entries always appear at the top
    top_text      = main.get_text()[:3000]
    current_hash  = hashlib.md5(top_text.encode()).hexdigest()
    last_hash     = last_seen.get(key)

    if current_hash == last_hash:
        print("  No changes detected")
        return None, current_hash

    print("  Change detected!")

    # Extract the first section as a summary
    headings = main.find_all(["h1", "h2", "h3"])
    title    = headings[0].get_text(strip=True) if headings else "New update detected"

    body_parts: list[str] = []
    if headings:
        for sib in headings[0].find_next_siblings():
            if sib.name in ("h1", "h2", "h3"):
                break
            t = sib.get_text(strip=True)
            if t:
                body_parts.append(t)

    body = "\n".join(body_parts[:6])
    return [{"title": title, "body": body, "url": url}], current_hash

# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"Claude Update Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*50}\n")

    last_seen     = load_last_seen()
    new_last_seen = dict(last_seen)
    sent          = 0

    # 1. Claude Code
    items, tag = check_claude_code(last_seen)
    if items:
        send_telegram(build_message(
            "💻", "Claude Code",
            items, "https://github.com/anthropics/claude-code/releases"
        ))
        sent += 1
    new_last_seen["claude_code"] = tag

    # 2. Anthropic News
    items, url = check_anthropic_news(last_seen)
    if items:
        send_telegram(build_message(
            "📢", "Anthropic News",
            items, "https://www.anthropic.com/news"
        ))
        sent += 1
    new_last_seen["anthropic_news"] = url

    # 3. Claude API / Models release notes
    items, h = check_page(
        "api_notes",
        "https://platform.claude.com/docs/en/release-notes/overview",
        "Claude API Notes",
        last_seen,
    )
    if items:
        send_telegram(build_message(
            "🔧", "Claude API & Models",
            items, "https://platform.claude.com/docs/en/release-notes/overview"
        ))
        sent += 1
    new_last_seen["api_notes"] = h

    # 4. Claude.ai (chat) release notes
    items, h = check_page(
        "claude_ai_notes",
        "https://support.claude.com/en/articles/12138966-release-notes",
        "Claude.ai Notes",
        last_seen,
    )
    if items:
        send_telegram(build_message(
            "💬", "Claude.ai",
            items, "https://support.claude.com/en/articles/12138966-release-notes"
        ))
        sent += 1
    new_last_seen["claude_ai_notes"] = h

    save_last_seen(new_last_seen)

    # ── Daily summary (always sent) ─────────────────────────────────────────────
    date_str = datetime.now().strftime("%B %d, %Y")

    summary_lines = [
        f"📋 <b>Daily Claude Update Report</b>",
        f"📅 {date_str}",
        "─────────────────────",
    ]

    statuses = {
        "claude_code":    ("💻 Claude Code",         tag),
        "anthropic_news": ("📢 Anthropic News",      url),
        "api_notes":      ("🔧 Claude API & Models", new_last_seen.get("api_notes")),
        "claude_ai_notes":("💬 Claude.ai",           new_last_seen.get("claude_ai_notes")),
    }

    if sent > 0:
        summary_lines.append(f"\n✅ <b>{sent} new update(s) found — see messages above.</b>")
    else:
        summary_lines.append("\n😴 <b>No new updates today.</b>")

    summary_lines += [
        "",
        "Sources checked:",
        "💻 Claude Code → github.com/anthropics/claude-code/releases",
        "📢 Anthropic News → anthropic.com/news",
        "🔧 Claude API → platform.claude.com/docs/en/release-notes/overview",
        "💬 Claude.ai → support.claude.com release notes",
    ]

    send_telegram("\n".join(summary_lines))

    print(f"\n{'='*50}")
    print(f"Done. {sent} update notification(s) + 1 daily summary sent.")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
