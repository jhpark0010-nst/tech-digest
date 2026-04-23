# tech-digest

개인용 기술 뉴스 다이제스트. 11개 RSS 소스(미국/중국/일본 영어·중국어·일본어 매체)에서 AI·스타트업·투자 관련 기사를 긁어서 한글 요약 + 원문 링크로 Slack에 보낸다.

## 구조

```
[cron-job.org]  KST 06:00/12:00/18:00/00:00
    ↓ workflow_dispatch
[GitHub Actions digest.yml]
    ↓ scripts/run_digest.py
    ├ 11개 RSS 수집 (최근 6시간)
    ├ 키워드 필터 (AI, 스타트업, 투자, M&A)
    ├ seen.json dedup
    ├ Claude 1회 호출 → 한글 요약
    ├ Slack Webhook 전송 (소스별 그룹핑)
    └ git commit & push (seen.json)
```

## 설정

GitHub Secrets:

| Key | 용도 |
|-----|------|
| `ANTHROPIC_API_KEY` | 필수 |
| `CLAUDE_MODEL` | 선택. 기본값 `claude-sonnet-4-6` |
| `SLACK_WEBHOOK_URL` | 필수. `#tech-digest` 등 개인 채널 |

cron-job.org:
- URL: `https://api.github.com/repos/{user}/tech-digest/actions/workflows/digest.yml/dispatches`
- Header: `Authorization: Bearer {PAT}` (repo + workflow scope)
- Body: `{"ref":"main"}`
- Schedule (KST): `0 6,12,18,0 * * *` → UTC `0 21,3,9,15 * * *`

## RSS 소스

영어(5): TechCrunch, Ars Technica AI, MIT Tech Review, VentureBeat AI, The Verge
중국 관련 영어(3): TechNode, SCMP Tech, SixthTone
일본어(3): Nikkei xTECH, ITmedia Enterprise, Gigazine

소스 추가/제거는 `config/settings.py` 의 `RSS_FEEDS` 수정.

## 로컬 실행

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
export SLACK_WEBHOOK_URL=...
python scripts/run_digest.py
```

`GITHUB_ACTIONS` 환경변수 없으면 git push 는 스킵. seen.json 만 로컬에 남음.
