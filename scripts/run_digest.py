"""tech-digest 메인 스크립트.

6시간마다 11개 RSS 피드에서 기사 수집 → 키워드 필터 → Claude로 한글 요약 → Slack 전송.
실행 후 seen.json 업데이트 + git commit/push.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anthropic
import feedparser
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (  # noqa: E402
    COUNTRY_FLAGS,
    KEYWORDS,
    LOOKBACK_HOURS,
    MAX_OUTPUT_TOKENS,
    RSS_FEEDS,
    SEEN_PATH,
    SEEN_RETENTION_DAYS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tech-digest")

KST = timezone(timedelta(hours=9))

# 키워드 분리: ASCII 는 단어 경계 regex, 비-ASCII(한중일)는 substring.
# 이유: "AI" 를 단순 substring 매칭하면 "said", "airport" 등에 걸려 노이즈 과다.
def _is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


_ASCII_KWS = [k for k in KEYWORDS if _is_ascii(k)]
_CJK_KWS = [k for k in KEYWORDS if not _is_ascii(k)]
# ASCII 키워드는 앞뒤가 단어문자가 아닌 위치에서만 매칭 (word boundary 확장판).
# \b 만으론 "Pre-A" 같은 하이픈 포함 키워드가 안 먹어서 lookaround 로 명시.
_ASCII_BOUNDARY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(k) for k in _ASCII_KWS) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# HTML 태그 제거용
_TAG_RE = re.compile(r"<[^>]+>")


def load_seen() -> dict[str, str]:
    """seen.json 로드. {guid: iso_date} 형태. 파일 없으면 빈 dict."""
    if not SEEN_PATH.exists():
        return {}
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.warning("seen.json 파싱 실패 — 빈 상태로 시작")
        return {}


def save_seen(seen: dict[str, str]) -> None:
    """오래된 GUID 제거 후 저장."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    pruned = {g: d for g, d in seen.items() if d >= cutoff}
    SEEN_PATH.write_text(
        json.dumps(pruned, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("seen.json 저장: %d건 (%d건 만료)", len(pruned), len(seen) - len(pruned))


def strip_html(text: str) -> str:
    if not text:
        return ""
    return _TAG_RE.sub("", text).strip()


def match_keyword(text: str) -> bool:
    """제목+요약에 KEYWORDS 하나라도 매칭되면 True.

    - ASCII 키워드: 단어 경계 매칭 (said 의 'ai', invoice 의 'vc' 오매칭 방지)
    - 한중일 키워드: substring 매칭 (이 언어는 단어 경계 개념 약함)
    """
    if not text:
        return False
    if _ASCII_BOUNDARY_RE.search(text):
        return True
    return any(kw in text for kw in _CJK_KWS)


def parse_entry_time(entry: Any) -> datetime | None:
    """feedparser entry 의 pubDate 파싱. 실패 시 None."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None) or entry.get(attr) if hasattr(entry, "get") else None
        if t:
            try:
                return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            except Exception:
                continue
    return None


def fetch_feed(name: str, meta: dict, since: datetime, seen: dict[str, str]) -> list[dict]:
    """단일 피드에서 신규 + 키워드 매칭 + 시간윈도우 통과한 entry 리스트 반환."""
    url = meta["url"]
    country = meta["country"]
    try:
        parsed = feedparser.parse(url)
    except Exception:
        log.exception("RSS fetch 실패: %s", name)
        return []

    if parsed.bozo and not parsed.entries:
        log.warning("RSS 파싱 에러: %s (%s)", name, parsed.bozo_exception)
        return []

    items: list[dict] = []
    for entry in parsed.entries:
        guid = entry.get("id") or entry.get("link")
        if not guid:
            continue
        if guid in seen:
            continue

        pub = parse_entry_time(entry)
        # pubDate 없으면 일단 통과 (매우 드문 케이스. 나중에 중복이면 seen 이 걸러줌)
        if pub and pub < since:
            continue

        title = strip_html(entry.get("title", ""))
        summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
        link = entry.get("link", "")

        if not title or not link:
            continue

        if not match_keyword(title + " " + summary):
            continue

        items.append({
            "source": name,
            "country": country,
            "guid": guid,
            "title": title,
            "summary": summary[:800],
            "link": link,
            "published": pub.isoformat() if pub else "",
        })

    log.info("%s: %d건 수집 (전체 %d엔트리)", name, len(items), len(parsed.entries))
    return items


def collect_all(seen: dict[str, str]) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    log.info("수집 윈도우: %s 이후 (%d시간)", since.isoformat(), LOOKBACK_HOURS)
    all_items: list[dict] = []
    for name, meta in RSS_FEEDS.items():
        all_items.extend(fetch_feed(name, meta, since, seen))
    log.info("전체 수집: %d건", len(all_items))
    return all_items


def summarize_batch(items: list[dict]) -> dict[str, str]:
    """Claude 1회 호출로 전체 item 한글 요약. {guid: summary_kr} 반환."""
    if not items:
        return {}

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 환경변수 미설정")

    # 빈 문자열도 default 로 대체 (Secret 미등록 시 ${{ secrets.X }} 가 "" 로 평가됨)
    model = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-sonnet-4-6"
    client = anthropic.Anthropic(api_key=api_key)

    # 프롬프트 조립 — guid → 한글 2~3문장 요약 JSON
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(
            f"[{i}] guid={it['guid']}\n"
            f"source={it['source']} ({it['country']})\n"
            f"title: {it['title']}\n"
            f"summary: {it['summary']}\n"
        )
    body = "\n---\n".join(lines)

    system = (
        "너는 기술 뉴스 편집자다. 각 기사를 한국어로 2~3문장(120자 이상 220자 이하)으로 요약한다.\n"
        "규칙:\n"
        "1) 원문에 충실. 없는 내용 추가 금지. 과장/해석 금지.\n"
        "2) 투자 라운드, 금액, 회사명, 기술명은 정확히 보존.\n"
        "3) 기자체. 건조한 서술체.\n"
        "4) 반드시 JSON 객체 1개만 반환. key = guid, value = 한글 요약 문자열."
    )
    user = (
        f"{len(items)}건의 기사를 요약해라. guid 를 그대로 key 로 쓴 JSON 만 반환.\n\n{body}"
    )

    log.info("Claude 호출: model=%s items=%d", model, len(items))
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = resp.content[0].text.strip()

    # JSON 추출 (앞뒤 ``` 제거)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.error("Claude 응답 JSON 파싱 실패. 원문 앞 500자: %s", text[:500])
        raise

    if not isinstance(parsed, dict):
        raise RuntimeError(f"Claude 응답이 dict 가 아님: {type(parsed)}")

    return {str(k): str(v) for k, v in parsed.items()}


def build_slack_blocks(items: list[dict], summaries: dict[str, str]) -> list[dict]:
    """Slack Block Kit 메시지 빌드. 소스별로 그룹핑."""
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Tech Digest · {len(items)}건 · {now_kst}"},
        }
    ]

    # 국가별 → 소스별 그룹핑
    by_source: dict[str, list[dict]] = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it)

    for source, src_items in by_source.items():
        country = src_items[0]["country"]
        flag = COUNTRY_FLAGS.get(country, "🌐")
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{flag} {source}* · {len(src_items)}건"},
        })
        for it in src_items:
            summary_kr = summaries.get(it["guid"], "(요약 없음)")
            text = (
                f"• <{it['link']}|{it['title']}>\n"
                f"  {summary_kr}"
            )
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": text[:2900]},  # Slack 3000자 제한 여유
            })

    return blocks


def post_to_slack(blocks: list[dict]) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        log.warning("SLACK_WEBHOOK_URL 미설정 — Slack 전송 스킵")
        return

    # Slack 은 한 메시지당 블록 50개 제한. 초과 시 분할.
    CHUNK = 48
    for i in range(0, len(blocks), CHUNK):
        chunk = blocks[i:i + CHUNK]
        payload = {"blocks": chunk}
        r = requests.post(webhook, json=payload, timeout=15)
        if r.status_code != 200:
            log.error("Slack 전송 실패 (chunk %d): %d %s", i // CHUNK, r.status_code, r.text[:200])
        else:
            log.info("Slack 전송 OK (chunk %d, blocks=%d)", i // CHUNK, len(chunk))


def post_empty_to_slack() -> None:
    """신규 0건일 때도 살아있다고 알리는 가벼운 메시지."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    payload = {
        "text": f"Tech Digest · {now_kst} · 신규 기사 없음 (최근 {LOOKBACK_HOURS}시간)",
    }
    try:
        r = requests.post(webhook, json=payload, timeout=15)
        if r.status_code != 200:
            log.error("Slack empty 알림 실패: %d %s", r.status_code, r.text[:200])
    except Exception:
        log.exception("Slack empty 알림 예외")


def git_commit_and_push() -> None:
    """seen.json 커밋+푸시. GitHub Actions 환경에서만 동작."""
    if not os.environ.get("GITHUB_ACTIONS"):
        log.info("로컬 실행 — git push 스킵")
        return

    repo_root = SEEN_PATH.parent.parent
    try:
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            cwd=repo_root, check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            cwd=repo_root, check=True,
        )
        subprocess.run(["git", "add", "data/seen.json"], cwd=repo_root, check=True)

        diff = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=repo_root,
        )
        if diff.returncode == 0:
            log.info("seen.json 변경 없음 — commit 스킵")
            return

        subprocess.run(
            ["git", "commit", "-m", f"digest: seen.json 업데이트 ({datetime.now(KST).isoformat(timespec='minutes')})"],
            cwd=repo_root, check=True,
        )
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=repo_root, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=repo_root, check=True)
        log.info("git push 성공")
    except subprocess.CalledProcessError as e:
        log.error("git 작업 실패: %s", e)


def main() -> None:
    seen = load_seen()
    log.info("seen.json 로드: %d건", len(seen))

    items = collect_all(seen)
    if not items:
        log.info("신규 기사 없음 — Slack empty 알림")
        post_empty_to_slack()
        return

    summaries = summarize_batch(items)

    blocks = build_slack_blocks(items, summaries)
    post_to_slack(blocks)

    # seen.json 업데이트
    now_iso = datetime.now(timezone.utc).isoformat()
    for it in items:
        seen[it["guid"]] = now_iso
    save_seen(seen)

    git_commit_and_push()


if __name__ == "__main__":
    main()
