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


def _try_repair_unescaped_quotes(text: str):
    """JSON string value 안 이스케이프 안 된 ASCII `"` 자동 escape 후 재파싱.

    blog-kpop/blog-automation 에서 검증된 휴리스틱 스캐너. 문자열 종료처럼 보이지만
    뒤에 구조 문자(`:`/`,`/`}`/`]`)가 따라오지 않으면 이스케이프 누락으로 간주.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            out.append(c)
            out.append(text[i + 1])
            i += 2
            continue
        if c == '"':
            if not in_string:
                in_string = True
                out.append(c)
                i += 1
                continue
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in ":,}]":
                in_string = False
                out.append(c)
                i += 1
                continue
            out.append("\\")
            out.append('"')
            i += 1
            continue
        out.append(c)
        i += 1
    try:
        return json.loads("".join(out))
    except json.JSONDecodeError:
        return None


def summarize_batch(items: list[dict]) -> dict[str, dict]:
    """Claude 1회 호출로 전체 item 한글 제목+요약 반환. {guid: {title_kr, summary_kr}}.

    JSON 파싱 실패 시 temperature 흔들며 최대 2회 재시도, 그래도 실패하면
    unescaped 큰따옴표 자동 복구 시도 (blog-kpop 동일 패턴).
    """
    if not items:
        return {}

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 환경변수 미설정")

    # 빈 문자열도 default 로 대체 (Secret 미등록 시 ${{ secrets.X }} 가 "" 로 평가됨)
    model = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-sonnet-4-6"
    client = anthropic.Anthropic(api_key=api_key)

    # 프롬프트 조립 — guid → {title_kr, summary_kr} JSON
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
        "너는 기술 뉴스 편집자다. 각 기사마다 한국어 제목과 요약을 생성한다.\n"
        "규칙:\n"
        "1) title_kr: 원문 제목을 자연스러운 한국어로 번역. 50자 이하. 회사명/제품명/고유명사는 널리 쓰이는 표기 유지 (예: OpenAI, Claude, GPT-5 는 영문 그대로).\n"
        "2) summary_kr: 한국어 2~3문장(120~220자). 기자체, 건조한 서술체.\n"
        "3) 원문에 충실. 없는 내용 추가 금지. 과장/해석 금지.\n"
        "4) 투자 라운드, 금액, 회사명, 기술명은 정확히 보존.\n"
        "5) 반드시 JSON 객체 1개만 반환. key=guid, value={\"title_kr\": \"...\", \"summary_kr\": \"...\"}.\n"
        "6) ⚠️ JSON 안의 문자열 값에 ASCII 큰따옴표(\")를 직접 넣지 말 것. "
        "원문 인용이 있으면 한국어 인용부호 \"…\" 또는 홑따옴표 '…' 로 치환. "
        "ASCII \" 하나가 이스케이프 안 되면 응답 전체 파싱이 깨짐."
    )
    user = (
        f"{len(items)}건의 기사를 번역+요약해라. guid 를 그대로 key 로 쓴 JSON 만 반환.\n\n{body}"
    )

    log.info("Claude 호출: model=%s items=%d", model, len(items))

    last_text: str | None = None
    last_err: Exception | None = None
    for attempt in range(3):  # 1회 원본 + 2회 재시도
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=min(0.3 + 0.1 * attempt, 1.0),
        )
        text = resp.content[0].text.strip()
        last_text = text

        # JSON 추출 (앞뒤 ``` 제거)
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        try:
            parsed = json.loads(text)
            break
        except json.JSONDecodeError as e:
            last_err = e
            log.warning("JSON 파싱 실패 (attempt %d/3): %s", attempt + 1, e)
    else:
        # 3회 모두 실패 — 마지막 응답 자동 escape 복구 시도
        if last_text is not None:
            stripped = last_text.strip()
            if stripped.startswith("```"):
                stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
                stripped = re.sub(r"\s*```$", "", stripped)
            repaired = _try_repair_unescaped_quotes(stripped)
            if repaired is not None:
                log.info("JSON 자동 복구 성공 (unescaped quotes)")
                parsed = repaired
            else:
                log.error("Claude 응답 JSON 파싱 최종 실패. 원문 앞 500자: %s", last_text[:500])
                raise last_err
        else:
            raise last_err

    if not isinstance(parsed, dict):
        raise RuntimeError(f"Claude 응답이 dict 가 아님: {type(parsed)}")

    # {guid: {title_kr, summary_kr}} 정규화. 구 버전(string value) 도 호환.
    out: dict[str, dict] = {}
    for k, v in parsed.items():
        if isinstance(v, dict):
            out[str(k)] = {
                "title_kr": str(v.get("title_kr", "")).strip(),
                "summary_kr": str(v.get("summary_kr", "")).strip(),
            }
        else:
            out[str(k)] = {"title_kr": "", "summary_kr": str(v).strip()}
    return out


def build_slack_blocks(items: list[dict], summaries: dict[str, dict]) -> list[dict]:
    """Slack Block Kit 메시지 빌드. 소스별로 그룹핑.

    제목 포맷: *<링크|한글 제목>* (굵은 링크)
    한글 제목 없으면 원문 제목으로 fallback.
    """
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
            entry = summaries.get(it["guid"], {})
            title_kr = entry.get("title_kr") or it["title"]
            summary_kr = entry.get("summary_kr") or "(요약 없음)"
            # Slack mrkdwn 은 링크 안 텍스트에 *bold* 가 안 먹음.
            # 대신 링크 전체를 *...* 로 감싸면 링크 문구가 굵게 표시됨.
            text = (
                f"• *<{it['link']}|{title_kr}>*\n"
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
