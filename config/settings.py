"""tech-digest 설정.

RSS 소스, 키워드 필터, 시간 윈도우 등.
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

SEEN_PATH = DATA_DIR / "seen.json"

# ── RSS 피드 11개 ──
# country_code: 슬랙 메시지에 국기 이모지로 표시
RSS_FEEDS = {
    # 영어 (5)
    "TechCrunch": {
        "url": "https://techcrunch.com/feed/",
        "country": "us",
    },
    "Ars Technica AI": {
        "url": "https://arstechnica.com/ai/feed/",
        "country": "us",
    },
    "MIT Tech Review": {
        "url": "https://www.technologyreview.com/feed/",
        "country": "us",
    },
    "VentureBeat AI": {
        "url": "https://venturebeat.com/category/ai/feed/",
        "country": "us",
    },
    "The Verge": {
        "url": "https://www.theverge.com/rss/index.xml",
        "country": "us",
    },
    # 중국 관련 영어 (3)
    "TechNode": {
        "url": "https://technode.com/feed/",
        "country": "cn",
    },
    "SCMP Tech": {
        "url": "https://www.scmp.com/rss/92/feed",
        "country": "hk",
    },
    "SixthTone": {
        "url": "https://www.sixthtone.com/rss",
        "country": "cn",
    },
    # 일본어 (3)
    "Nikkei xTECH": {
        "url": "https://xtech.nikkei.com/rss/xtech-it.rdf",
        "country": "jp",
    },
    "ITmedia Enterprise": {
        "url": "https://rss.itmedia.co.jp/rss/2.0/enterprise.xml",
        "country": "jp",
    },
    "Gigazine": {
        "url": "https://gigazine.net/news/rss_2.0/",
        "country": "jp",
    },
}

# 국가 코드 → 이모지
COUNTRY_FLAGS = {
    "us": "🇺🇸",
    "uk": "🇬🇧",
    "cn": "🇨🇳",
    "hk": "🇭🇰",
    "tw": "🇹🇼",
    "jp": "🇯🇵",
}

# ── 주제 필터 키워드 (AI · 스타트업 · 투자) ──
# 대소문자 무시. 제목+요약에 하나라도 매칭되면 통과.
KEYWORDS = [
    # AI / ML (영어)
    "AI", "artificial intelligence", "machine learning", "deep learning",
    "LLM", "GPT", "Claude", "Anthropic", "OpenAI", "Gemini", "Meta AI",
    "neural network", "transformer", "diffusion", "foundation model",
    "generative AI", "agent", "AGI",
    # AI / ML (한중일)
    "생성AI", "생성형", "인공지능", "머신러닝", "딥러닝",
    "人工智能", "机器学习", "深度学习", "生成式",
    "人工知能", "生成AI", "機械学習",
    # 스타트업 / 창업 (영어)
    "startup", "founder", "YC", "Y Combinator",
    # 스타트업 / 창업 (한중일)
    "스타트업", "창업", "创业", "初创",
    "スタートアップ", "起業",
    # 투자 라운드
    "seed round", "pre-seed", "Seed", "Pre-A", "Pre-Seed",
    "Series A", "Series B", "Series C", "Series D", "Series E",
    "funding round", "raised", "raises",
    "bridge round", "extension round", "down round",
    "valuation", "unicorn", "IPO", "exit",
    "venture capital", "VC",
    # 투자 (한중일)
    "투자유치", "투자 유치", "펀딩", "시드", "프리A", "시리즈A", "시리즈B",
    "융자", "融资", "轮融资", "天使轮", "种子轮", "A轮", "B轮",
    "資金調達", "シード", "シリーズA", "シリーズB",
    # M&A / 인수
    "acquisition", "acquires", "merger", "buyout",
    "인수", "합병", "收购", "并购", "買収", "合併",
]

# ── 시간 윈도우 ──
# 매 cron 실행 시 이 시간만큼 되돌아가 기사 필터링
LOOKBACK_HOURS = 6

# ── 중복 방지: 최근 N일 GUID 보존 ──
SEEN_RETENTION_DAYS = 14

# ── Claude API ──
# 26+건 한 번에 요약 시 4000 토큰으로 응답 중간 잘림 (Unterminated string).
# Sonnet 4.6 최대 출력 8192 까지 가능. 여유롭게 8000.
MAX_OUTPUT_TOKENS = 8000
