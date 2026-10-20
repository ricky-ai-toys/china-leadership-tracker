#!/usr/bin/env python3
"""Xinwen Lianbo (CCTV) daily-broadcast scanner and site-data builder.

Self-contained, stdlib-only. Rebuilds the XWLB half of the public dataset
now that the private pipeline repo (nelsonwang222/china-leadership-tracker-
pipeline) is permanently unreachable and data froze at 2026-08-02.

Source: tv.cctv.com/lm/xwlb/day/YYYYMMDD.shtml lists that day's broadcast
segments (tv.cctv.com/YYYY/MM/DD/VIDE*.shtml); each segment page carries the
segment title plus its full transcript text. The scanner fetches every
segment of every missing day, classifies it into the site's existing event
types, tags leaders from the public leaders.json roster, translates new rows
via the repo's Anthropic/Gemini/DeepSeek secrets, and merges the results
into the published docs/data/ files WITHOUT touching the 2016-2026-08
archive rows or the rmrb (People's Daily) shards.

State (both committed, append-only):
  xwlb/records.jsonl       one raw segment per line (fetched once)
  xwlb/translations.jsonl  one translation per line (cached forever)
  xwlb/checked.json        empty-date memory, TTL 7 days

Usage:
  python scripts/xwlb_build.py [--since 2026-08-03] [--until YYYY-MM-DD]
      [--max-days N] [--translate-limit N] [--no-translate] [--dry-run]
      [--sleep SECONDS]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SINCE = "2026-08-03"  # day after the last published xwlb event
DEFAULT_CHECKED_TTL_DAYS = 7
DEFAULT_RECORDS_DIR = ROOT / "xwlb"
DEFAULT_OUT_DIR = ROOT / "docs" / "data"
DEFAULT_LEADERS = ROOT / "docs" / "data" / "leaders.json"
XWLB_BASE = "https://tv.cctv.com/lm/xwlb"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
INDEX_COLUMNS = [
    "id", "date", "type", "activity", "title_zh", "title_en", "summary_en",
    "leaders", "mentions", "counterpart", "location",
]
# Fallback copy of index.json's types map; published labels win when present.
TYPE_LABELS = {
    "meeting": "Meeting with guests",
    "talks": "Bilateral talks",
    "phone_call": "Phone call",
    "message": "Message / letter",
    "chaired_meeting": "Leadership meeting",
    "inspection": "Inspection tour",
    "foreign_trip": "Foreign trip",
    "visit_to_china": "Visit to China",
    "ceremony": "Ceremony / event",
    "symposium": "Symposium",
    "speech": "Speech / instructions",
    "deliberation": "NPC/CPPCC deliberation",
    "funeral": "Funeral / condolence",
    "decree": "Appointment",
    "document": "Policy document",
    "article": "Published article",
    "brief": "News brief",
    "coverage": "Coverage / reaction",
    "other": "Other",
}
# Types that describe a leader's own activity rather than published/reaction
# material. Mirrors the old pipeline's activity flag for these types.
ACTIVE_TYPES = {
    "meeting", "talks", "phone_call", "message", "chaired_meeting",
    "inspection", "foreign_trip", "visit_to_china", "ceremony", "symposium",
    "speech", "deliberation", "funeral", "decree",
}
# Names immediately followed by one of these are ideology boilerplate
# (e.g. 习近平新时代中国特色社会主义思想), not a report of the person's activity.
BOILER_PHRASES = ("习近平新时代中国特色社会主义思想", "新时代", "思想", "精神", "理论", "战略")
BEIJING = timezone(timedelta(hours=8))

# Transcripts end with this footer line; drop it (and anything after it).
FOOTER_MARK = "京ICP备"


class BlockedError(RuntimeError):
    """CCTV refused us (403); stop fetching, self-heal next run."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def beijing_today() -> date:
    return datetime.now(BEIJING).date()


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"WARN skipping malformed line in {path}", file=sys.stderr)
    return out


def save_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def load_checked(path: Path) -> dict[str, str]:
    """Return {date: checked_at_iso} for dates found empty on a prior run."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_checked(path: Path, checked: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(checked, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(path)


def checked_stale(checked_at: str, ttl_days: int, now: datetime) -> bool:
    try:
        dt = datetime.fromisoformat(checked_at)
    except (TypeError, ValueError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt) > timedelta(days=ttl_days)


def plan_missing_dates(
    since: date,
    until: date,
    known_dates: set[str],
    checked: dict[str, str],
    ttl_days: int,
    max_days: int,
) -> list[date]:
    """Dates to fetch: not already recorded, not recently checked-empty."""
    now = datetime.now(timezone.utc)
    out: list[date] = []
    for i in range((until - since).days + 1):
        d = since + timedelta(days=i)
        iso = d.isoformat()
        if iso in known_dates:
            continue
        if iso in checked and not checked_stale(checked[iso], ttl_days, now):
            continue
        out.append(d)
        if len(out) >= max_days:
            break
    return out


def fetch_url(url: str, timeout: int = 30, retries: int = 3) -> tuple[int, bytes]:
    """Return (status, body). Raises BlockedError on 403, URLError after retries."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
    }
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise BlockedError(f"{url} -> 403 (anti-bot; stopping fetch)")
            if e.code == 404:
                return 404, b""
            last_err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
        if attempt + 1 < retries:
            time.sleep(0.5 * (2 ** attempt))
    raise last_err or RuntimeError(f"fetch failed: {url}")


def day_url(d: date) -> str:
    return f"{XWLB_BASE}/day/{d:%Y%m%d}.shtml"


def strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# tv.cctv.com/2026/10/20/VIDExxxx261020.shtml — the trailing digits echo the date
SEGMENT_URL_RE = re.compile(
    r"https://tv\.cctv\.com/(\d{4})/(\d{2})/(\d{2})/VIDE[A-Za-z0-9]+(\d{8})\.shtml"
)


def parse_day_index(html_text: str, day: date) -> list[dict]:
    """Return [{url, title}] for one day's broadcast page, in page order.

    The page lists the full-episode link plus every segment; the same URL can
    appear in several anchors, so dedupe by URL keeping the first title.
    """
    items: dict[str, dict] = {}
    pattern = re.compile(
        r'<a\b[^>]*\bhref="(https://tv\.cctv\.com/\d{4}/\d{2}/\d{2}/'
        r'VIDE[A-Za-z0-9]+\.shtml)"[^>]*\btitle="([^"]*)"',
        re.S | re.I,
    )
    for href, title in pattern.findall(html_text):
        title = title.strip()
        if title.startswith("[视频]"):
            title = title[len("[视频]"):].strip()
        if not title or href in items:
            continue
        items[href] = {"url": href, "title": normalize_space(title)}
    # Drop the placeholder "完整版" (full-version) anchor: its href duplicates
    # the first segment's URL, so after dedupe nothing extra is needed here.
    return [v for k, v in items.items()]


def parse_segment(html_text: str, fallback_title: str = "") -> tuple[str, str]:
    """Return (title_zh, content_zh) from one segment page.

    Body lives in <div class="content_area" id="content_area"><p>..</p>.. and
    ends at the site footer (京ICP备…)."""
    m = re.search(r"<title>(.*?)</title>", html_text, re.S)
    title = normalize_space(strip_tags(m.group(1))) if m else ""
    if title.startswith("[视频]"):
        title = title[len("[视频]"):].strip()
    title = title or fallback_title

    marker = html_text.find("content_area")
    body = html_text[marker:] if marker >= 0 else html_text
    paras: list[str] = []
    for p in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S):
        text = normalize_space(strip_tags(p))
        if not text:
            continue
        if FOOTER_MARK in text or "版权所有" in text:
            break
        paras.append(text)
    return title, "\n".join(paras)


_CLASSIFY_BRIEF = ("联播快讯", "快讯", "新闻速览")
_CLASSIFY_COVERAGE = ("纪实", "启示", "反响", "巡礼", "足迹", "新思想引领",
                      "学习", "解读", "专家", "观察", "综述", "反响",
                      "央视快评", "央视网评", "新华时评", "人民时评", "评论")
_CLASSIFY_SYM = ("座谈会", "研讨", "论坛", "峰会", "会晤", "理事会", "大会",
                 "会议", "博览会", "开幕式")
_CLASSIFY_DECREE = ("签署命令", "签署主席令", "通令", "任免", "免去",
                      "任命", "记功", "授予", "主席令")
_CLASSIFY_MESSAGE = ("致信", "致电", "贺电", "贺信", "慰问电", "回信", "复信",
                     "唁电", "表示感谢")

_CLASSIFY_VISIT = ("访华", "来访", "访问中国", "对中国", "来华", "抵华",
                   "抵达北京", "抵京")
_CLASSIFY_TRIP = ("出访", "将访问", "将赴", "将对", "并访问", "启程", "回国",
                 "专机")
_CLASSIFY_ATTEND = ("会议", "大会", "年会", "论坛", "峰会")
_CLASSIFY_DELIBERATION = ("人大", "政协", "两会", "代表团", "全体会议", "审议")
_CLASSIFY_PHONE = ("通电话", "通话", "电话")
_CLASSIFY_TALKS = ("会谈", "会商", "磋商")
_CLASSIFY_SPEECH = ("讲话", "致辞", "演讲", "指示", "强调", "发表")


def classify_type(title: str, content: str) -> str:
    """Map a segment to one of the site's 17 event types (heuristic).

    Rule order and mappings follow the 2016-2026 archive's own distributions
    (e.g. 出席+会议/年会 -> ceremony, 主持+会议 -> chaired_meeting,
    会晤 -> meeting), so new rows blend into the published dataset.
    """
    t = title
    if any(k in t for k in _CLASSIFY_BRIEF):
        return "brief"
    if "《" in t or "求是" in t or ("文章" in t and "发表" in t):
        return "article"
    if any(k in t for k in _CLASSIFY_COVERAGE) and "会见" not in t:
        return "coverage"
    if t.startswith("【") and "】" in t and "会见" not in t:
        return "coverage"
    if any(k in t for k in ("印发", "意见", "规划", "纲要", "条例", "法规")):
        if "会见" not in t and "会谈" not in t:
            return "document"
    if any(k in t for k in _CLASSIFY_DECREE):
        return "decree"
    if any(k in t for k in ("逝世", "悼念", "哀悼", "送别")):
        return "funeral"
    if any(k in t for k in _CLASSIFY_MESSAGE):
        return "message"
    # Incoming visits (a foreign figure coming to China) first ...
    if any(k in t for k in _CLASSIFY_VISIT):
        return "visit_to_china"
    if "应邀" in t and ("访" in t or "来" in t):
        return "visit_to_china"
    # ... then outgoing trips / attendance abroad
    if any(k in t for k in _CLASSIFY_TRIP):
        return "foreign_trip"
    if re.search(r"主持", t) and ("会议" in t or "学习" in t or "工作" in t):
        return "chaired_meeting"
    if any(k in t for k in _CLASSIFY_PHONE):
        return "phone_call"
    if any(k in t for k in _CLASSIFY_TALKS):
        return "talks"
    if any(k in t for k in ("会见", "接见", "会晤")):
        return "meeting"
    if any(k in t for k in ("考察", "调研", "视察", "走访", "慰问")):
        return "inspection"
    if any(k in t for k in _CLASSIFY_DELIBERATION):
        return "deliberation"
    if "欢迎" in t and ("仪式" in t or "来访" in t or "访华" in t):
        return "ceremony"
    if any(k in t for k in ("仪式", "颁奖", "授勋", "庆贺")):
        return "ceremony"
    # Attending an event/meeting reads as ceremony in the archive
    if "出席" in t and any(k in t for k in _CLASSIFY_ATTEND):
        return "ceremony"
    if any(k in t for k in _CLASSIFY_SPEECH):
        return "speech"
    if any(k in t for k in _CLASSIFY_SYM):
        return "symposium"
    return "other"


_COUNTERPART_PATTERNS = [
    re.compile(r"会见(?:并亲切会见)?([^\s，。；：、]{2,20}?)(?:$|，|。|；|、)"),
    re.compile(r"同([^\s，。；：、]{2,20}?)(?:通电话|通话|举行会谈|会谈)"),
    re.compile(r"与([^\s，。；：、]{2,20}?)(?:举行会谈|会谈|会晤)"),
    re.compile(r"欢迎([^\s，。；：、]{2,20}?)(?:访华|来访|抵达|到访)"),
    re.compile(r"接受([^\s，。；：、]{2,20}?)(?:采访|专访)"),
]
_COUNTERPART_STOPS = ("会议", "论坛", "组织", "峰会", "代表团", "外长", "总统",
                      "总理", "国王", "首相", "主席", "议长", "秘书长",
                      "国际", "联合国", "使节", "客人")
_COUNTERPART_VERBS = ("出席", "主持", "举行", "召开", "参加", "发表", "强调",
                      "表示", "指出", "认为", "说", "说，", "会见", "会谈")


def extract_counterpart(title: str) -> str:
    """Best-effort counterpart extraction (Chinese) from a segment title."""
    for pat in _COUNTERPART_PATTERNS:
        m = pat.search(title)
        if not m:
            continue
        cand = m.group(1).strip()
        if not cand or any(v in cand for v in _COUNTERPART_VERBS):
            continue
        if len(cand) > 20:
            continue
        return cand
    return ""


def has_real_mention(text: str, name: str) -> bool:
    for m in re.finditer(re.escape(name), text):
        tail = text[m.end():m.end() + 14]
        if any(tail.startswith(s) for s in BOILER_PHRASES):
            continue
        return True
    return False


def tag_leaders(record: dict, leaders: list[dict]) -> None:
    """Title hits become actors; content-only hits become mentions."""
    title = record.get("title_zh") or ""
    content = record.get("content_zh") or ""
    actors, mentions = [], []
    for p in leaders:
        name = (p.get("name_zh") or "").strip()
        if not name:
            continue
        if name in title:
            actors.append(p["id"])
        elif has_real_mention(content, name):
            mentions.append(p["id"])
    record["leaders"] = actors
    record["mentions"] = mentions


def fetch_records_for_date(
    d: date, leaders: list[dict], sleep: float
) -> tuple[list[dict], int]:
    """Return (records, expected_segment_count) for one broadcast date.

    expected is 0 when the day page is 404/empty, and the number of listed
    segments when the day parsed but some segment pages failed — callers use
    it to keep incomplete dates retryable.
    """
    url = day_url(d)
    status, body = fetch_url(url)
    if status == 404:
        return [], 0
    items = parse_day_index(body.decode("utf-8", "replace"), d)
    if not items:
        return [], 0
    # The full-episode player entry has no transcript; it is not a segment.
    items = [i for i in items if not i["title"].startswith("\u300a\u65b0\u95fb\u8054\u64ad\u300b")]
    time.sleep(sleep)
    records: list[dict] = []
    for n, item in enumerate(items):
        status, body = fetch_url(item["url"])
        if status == 404:
            # CCTV sometimes rate-limits with 404s; one quiet retry first.
            time.sleep(sleep * 2)
            status, body = fetch_url(item["url"])
        if status == 404:
            continue
        title, content = parse_segment(
            body.decode("utf-8", "replace"), item["title"]
        )
        if not content or len(content) < 40:
            continue
        record = {
            "id": f"{d:%Y%m%d}-{n:02d}",
            "date": d.isoformat(),
            "title_zh": title or item["title"],
            "url": item["url"],
            "content_zh": content,
            "type": classify_type(title or item["title"], content),
            "counterpart": extract_counterpart(title or item["title"]),
            "location": "",
            "leaders": [],
            "mentions": [],
            "fetched_at": now_iso(),
        }
        tag_leaders(record, leaders)
        records.append(record)
        time.sleep(sleep)
    return records, len(items)


def translate_with_anthropic(
    title_zh: str, content_zh: str, api_key: str, model: str
) -> dict | None:
    prompt = _translate_prompt(title_zh, content_zh)
    payload = {
        "model": model,
        "max_tokens": 500,
        "system": (
            "You are the translation engine for the China Leadership Tracker, "
            "a research database of Chinese leaders' public activities. "
            "Translate accurately and neutrally. Reply with JSON only."
        ),
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["content"][0]["text"]
    return parse_translation_json(text)


def translate_with_gemini(
    title_zh: str, content_zh: str, api_key: str, model: str
) -> dict | None:
    prompt = _translate_prompt(title_zh, content_zh)
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model)}:generateContent?key={api_key}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return parse_translation_json(text)


def translate_with_deepseek(
    title_zh: str, content_zh: str, api_key: str, model: str
) -> dict | None:
    prompt = _translate_prompt(title_zh, content_zh)
    payload = {
        "model": model,
        "max_tokens": 500,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the translation engine for the China Leadership "
                    "Tracker, a research database of Chinese leaders' public "
                    "activities. Translate accurately and neutrally. Reply "
                    "with JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    return parse_translation_json(text)


def _translate_prompt(title_zh: str, content_zh: str) -> str:
    return (
        "Translate this Xinwen Lianbo (CCTV evening news) broadcast segment "
        "for an English-language China leadership tracker.\n\n"
        f"Original headline: {title_zh}\n\n"
        f"Transcript text:/n{content_zh[:4000]}/n/n"
        'Reply with ONLY a JSON object: {"title_en": "...", "summary_en": "..."} '
        "where title_en is a fluent English headline and summary_en is a "
        "1-2 sentence factual summary of what the segment reports. If the "
        "segment mentions a foreign leader, name them in English in both "
        "fields."
    )


def parse_translation_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if data.get("title_en") and data.get("summary_en"):
                return {
                    "title_en": str(data["title_en"]).strip(),
                    "summary_en": str(data["summary_en"]).strip(),
                }
        except json.JSONDecodeError:
            pass
    m = re.search(r'"title_en"\s*:\s*"([^"]*)"', text)
    s = re.search(r'"summary_en"\s*:\s*"([^"]*)"', text)
    if m and s:
        return {"title_en": m.group(1), "summary_en": s.group(1)}
    return None


def translate_new_records(
    records: list[dict],
    translations: dict[str, dict],
    limit: int,
) -> int:
    """Translate records missing a cache entry; return number attempted."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    model = os.environ.get("TRANSLATE_MODEL", "").strip()
    deepseek_model = os.environ.get("DEEPSEEK_MODEL", "").strip() or "deepseek-chat"
    if limit <= 0:
        return 0
    if not (anthropic_key or gemini_key or deepseek_key):
        print("No translation API key set; skipping translation.")
        return 0
    use_anthropic = bool(anthropic_key)
    use_gemini = bool(gemini_key and not anthropic_key)
    use_deepseek = not use_anthropic and not use_gemini
    model = model or (
        "claude-opus-4-8" if use_anthropic
        else "gemini-2.5-flash" if use_gemini
        else deepseek_model
    )
    pending = [r for r in records if r["id"] not in translations]
    pending.sort(key=lambda r: (r["date"], r["id"]), reverse=True)  # newest first
    attempted = 0
    for r in pending[:limit]:
        try:
            if use_anthropic:
                t = translate_with_anthropic(r["title_zh"], r["content_zh"], anthropic_key, model)
            elif use_gemini:
                t = translate_with_gemini(r["title_zh"], r["content_zh"], gemini_key, model)
            else:
                t = translate_with_deepseek(r["title_zh"], r["content_zh"], deepseek_key, model)
        except Exception as e:  # noqa: BLE001 - keep going; next run retries
            print(f"WARN translate {r['id']} failed: {e}", file=sys.stderr)
            attempted += 1
            continue
        if t:
            translations[r["id"]] = {
                "id": r["id"],
                "title_en": t["title_en"],
                "summary_en": t["summary_en"],
                "model": model,
                "translated_at": now_iso(),
            }
        attempted += 1
    return attempted


def load_published(out_dir: Path) -> tuple[dict, dict, dict, dict]:
    """Read the published docs/data set: (rows_by_id, shards, labels, meta)."""
    rows: dict[str, list] = {}
    shards: dict[str, dict] = {}
    labels: dict[str, str] = {}
    meta: dict = {}
    idx_path = out_dir / "index.json"
    if idx_path.exists():
        data = json.loads(idx_path.read_text(encoding="utf-8"))
        labels = data.get("types") or {}
        for row in data.get("events", []):
            if row and isinstance(row, list):
                rows[row[0]] = row
    for p in sorted(out_dir.glob("events-*.json")):
        year = p.stem.split("-")[-1]
        shards[year] = json.loads(p.read_text(encoding="utf-8"))
    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return rows, shards, labels, meta


def default_since(meta: dict) -> str:
    """Day after the newest published xwlb event, so backfill is continuous."""
    last = (meta or {}).get("last_date") or ""
    try:
        return (date.fromisoformat(last) + timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return DEFAULT_SINCE


def build_outputs(
    records: list[dict],
    translations: dict[str, dict],
    out_dir: Path,
) -> None:
    """Merge new records into the published index/shards/meta; never rewrite
    or drop existing rows, and never touch docs/data/rmrb/**."""
    rows, shards, labels, _meta = load_published(out_dir)
    types_map = dict(TYPE_LABELS)
    types_map.update(labels)
    n_new = 0
    n_refilled = 0
    for r in records:
        tr = translations.get(r["id"], {})
        if r["id"] in rows:
            # A row published before its translation landed (API hiccup) still
            # needs its English fields; fill them in without touching the
            # rest of the row. Archive rows are never affected.
            existing = rows[r["id"]]
            if tr.get("title_en") and not existing[5]:
                existing[5] = tr["title_en"]
                existing[6] = tr.get("summary_en", "")
                n_refilled += 1
            continue
        rows[r["id"]] = [
            r["id"], r["date"], r["type"],
            1 if r["type"] in ACTIVE_TYPES else 0,
            r.get("title_zh", ""), tr.get("title_en", ""),
            tr.get("summary_en", ""),
            ",".join(r.get("leaders") or []),
            ",".join(r.get("mentions") or []),
            r.get("counterpart", ""), r.get("location", ""),
        ]
        shards.setdefault(r["date"][:4], {})[r["id"]] = {
            "content_zh": r.get("content_zh", ""),
            "url": r.get("url", ""),
        }
        n_new += 1
    all_rows = sorted(rows.values(), key=lambda x: (x[1], x[0]))
    dates = [r[1] for r in all_rows]
    n_translated = sum(1 for r in all_rows if r[5])
    out_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "columns": INDEX_COLUMNS,
        "types": types_map,
        "events": all_rows,
    }
    with (out_dir / "index.json").open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    for year in shards:
        with (out_dir / f"events-{year}.json").open("w", encoding="utf-8") as f:
            json.dump(shards[year], f, ensure_ascii=False, separators=(",", ":"))
    meta = {
        "built_at": now_iso(),
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
        "n_events": len(all_rows),
        "n_translated": n_translated,
        "n_days": len(set(dates)),
        "years": sorted({d[:4] for d in dates}),
    }
    with (out_dir / "meta.json").open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
    print(
        f"Built docs/data: {meta['n_events']} events (+{n_new} new, "
        f"{n_refilled} refilled), {meta['n_days']} days, "
        f"{meta['n_translated']} translated, last_date {meta['last_date']}"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", default=None,
                   help="backfill start date (default: day after published last_date)")
    p.add_argument("--until", default=None,
                   help="backfill end date (default: today, Beijing)")
    p.add_argument("--max-days", type=int, default=30,
                   help="max dates to fetch per run (guard against blocked runs)")
    p.add_argument("--translate-limit", type=int, default=300,
                   help="max segments to translate this run (0 = skip)")
    p.add_argument("--no-translate", action="store_true",
                   help="shortcut for --translate-limit 0")
    p.add_argument("--dry-run", action="store_true",
                   help="skip fetching/translation; rebuild outputs from records")
    p.add_argument("--checked-ttl-days", type=int, default=DEFAULT_CHECKED_TTL_DAYS,
                   help="re-check an empty date after this many days (default 7)")
    p.add_argument("--sleep", type=float, default=0.5,
                   help="seconds between requests (polite fetching)")
    p.add_argument("--records-dir", type=Path, default=DEFAULT_RECORDS_DIR)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--leaders", type=Path, default=DEFAULT_LEADERS)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    leaders = json.loads(args.leaders.read_text(encoding="utf-8"))
    records_path = args.records_dir / "records.jsonl"
    translations_path = args.records_dir / "translations.jsonl"
    records = load_jsonl(records_path)
    translations = {t["id"]: t for t in load_jsonl(translations_path)}
    known_ids = {r["id"] for r in records}
    known_dates = {r["date"] for r in records}
    checked_path = args.records_dir / "checked.json"
    checked = load_checked(checked_path)

    if not args.dry_run:
        _, _, _, meta = load_published(args.out_dir)
        since = date.fromisoformat(args.since or default_since(meta))
        until = date.fromisoformat(args.until or beijing_today().isoformat())
        missing = plan_missing_dates(
            since, until, known_dates, checked, args.checked_ttl_days, args.max_days
        )
        consecutive_misses = 0
        pending_checked: dict[str, str] = {}
        stopped_by_block = False
        for d in missing:
            try:
                fetched, expected = fetch_records_for_date(d, leaders, args.sleep)
            except BlockedError as e:
                print(f"{e}; keeping progress, next run resumes.")
                stopped_by_block = True
                break
            except Exception as e:  # one bad date shouldn't abort the backfill
                print(f"WARN {d} fetch failed: {e}", file=sys.stderr)
                continue
            if not fetched:
                if expected > 0:
                    print(f"WARN {d} day page parsed but no segments fetched; will retry")
                    continue
                consecutive_misses += 1
                # Never tombstone today: the evening broadcast may still be
                # in progress; the next run must retry it.
                if d < beijing_today():
                    pending_checked[d.isoformat()] = now_iso()
                print(f"{d} no items (404/empty); miss {consecutive_misses}")
                if consecutive_misses >= 5:
                    stopped_by_block = True
                    print("Stopping after 5 consecutive misses; next run resumes.")
                    break
                time.sleep(min(args.sleep * (2 ** consecutive_misses), 10))
                continue
            consecutive_misses = 0
            new = [r for r in fetched if r["id"] not in known_ids]
            if len(fetched) < expected:
                print(
                    f"WARN {d} incomplete ({len(fetched)}/{expected} segments); "
                    "date stays retryable"
                )
            else:
                known_dates.add(d.isoformat())
                checked.pop(d.isoformat(), None)
            records.extend(new)
            known_ids.update(r["id"] for r in new)
            save_jsonl(records_path, records)
            print(f"{d} +{len(new)} segments (total {len(records)})")
        if not stopped_by_block and pending_checked:
            checked.update(pending_checked)
            save_checked(checked_path, checked)
            print(f"Recorded {len(pending_checked)} checked-empty date(s).")

        if not args.no_translate:
            attempted = translate_new_records(
                records, translations, args.translate_limit
            )
            if attempted:
                save_jsonl(translations_path, list(translations.values()))
                print(f"Translated/attempted {attempted} new segments.")

    if records:
        build_outputs(records, translations, args.out_dir)
    else:
        print("No XWLB records yet; nothing to build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
