import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests
from google import genai

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "news.json"
HISTORY_DIR = ROOT / "history"
HISTORY_INDEX = HISTORY_DIR / "index.json"
KST = ZoneInfo("Asia/Seoul")

# 검색은 무료 RSS가 담당하고, Gemini는 "선별/요약"만 담당합니다.
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

RSS_QUERIES = [
    "artificial intelligence AI when:1d",
    "OpenAI OR ChatGPT when:1d",
    "Google Gemini OR DeepMind when:1d",
    "Anthropic Claude when:1d",
    "NVIDIA AI OR AI chip when:1d",
    "\"on-device AI\" OR NPU OR \"small language model\" when:2d",
    "Android AI OR mobile AI when:2d",
]

MAX_PER_FEED = 12
MAX_CANDIDATES = 55


def google_news_rss(query: str) -> str:
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )


def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def collect_articles():
    articles = []
    seen = set()

    for query in RSS_QUERIES:
        feed = feedparser.parse(google_news_rss(query))

        for entry in feed.entries[:MAX_PER_FEED]:
            title = clean_html(entry.get("title", ""))
            link = entry.get("link", "").strip()
            summary = clean_html(entry.get("summary", ""))
            published = entry.get("published", "")
            source = ""

            source_obj = entry.get("source")
            if isinstance(source_obj, dict):
                source = source_obj.get("title", "") or ""

            # Google News 제목 끝에 붙는 "- 매체명"을 출처 힌트로 사용
            if not source and " - " in title:
                possible_title, possible_source = title.rsplit(" - ", 1)
                if possible_source and len(possible_source) < 80:
                    title = possible_title.strip()
                    source = possible_source.strip()

            if not title or not link:
                continue

            dedupe_key = re.sub(r"[^a-z0-9가-힣]", "", title.lower())[:120]
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            articles.append({
                "title": title,
                "source": source or "Google News",
                "published": published,
                "summary": summary[:600],
                "url": link,
            })

            if len(articles) >= MAX_CANDIDATES:
                return articles

    return articles


def strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else text


def extract_json(text: str) -> dict:
    cleaned = strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("Gemini 응답에서 JSON을 찾지 못했습니다.")
        return json.loads(cleaned[start:end + 1])


def validate_ai_result(data: dict, candidates: list[dict]) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Gemini 결과 형식이 올바르지 않습니다.")

    issues = data.get("issues")
    if not isinstance(issues, list) or len(issues) < 4:
        raise ValueError("Gemini가 충분한 이슈를 반환하지 않았습니다.")

    # Gemini가 원문 URL을 새로 지어내지 못하게 candidate_id로 매칭
    by_id = {str(i + 1): item for i, item in enumerate(candidates)}
    output = []

    for item in issues[:5]:
        cid = str(item.get("candidate_id", "")).strip()
        src = by_id.get(cid)
        if not src:
            continue

        output.append({
            "category": str(item.get("category", "AI")).strip() or "AI",
            "title": str(item.get("title", src["title"])).strip() or src["title"],
            "summary": str(item.get("summary", "")).strip(),
            "detail": str(item.get("detail", "")).strip(),
            "why_it_matters": str(item.get("why_it_matters", "")).strip(),
            "source_name": src["source"],
            "source_url": src["url"],
            "published_date": str(item.get("published_date", "")).strip(),
        })

    if len(output) < 4:
        raise ValueError("유효한 이슈 매칭이 4개 미만입니다.")

    return {
        "daily_summary": str(data.get("daily_summary", "")).strip(),
        "issues": output[:5],
    }


def summarize_with_gemini(candidates: list[dict]) -> dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY가 없습니다.")

    compact = []
    for i, a in enumerate(candidates, 1):
        compact.append({
            "candidate_id": i,
            "title": a["title"],
            "source": a["source"],
            "published": a["published"],
            "summary": a["summary"],
        })

    today = datetime.now(KST).strftime("%Y-%m-%d")

    prompt = f"""
오늘은 {today}이다.

아래 뉴스 후보는 Google News RSS에서 최근 1~2일 범위로 수집한 AI 기사들이다.
웹 검색은 하지 말고, 제공된 후보만 사용해서 오늘 알아둘 핵심 AI 이슈 5개를 선정하고 한국어로 요약하라.

[선정 기준]
- 실제 영향도가 큰 발표/사건 우선
- 같은 사건의 중복 기사 제거
- 단순 의견, 광고성, 루머성 제목은 제외
- OpenAI/Google/Anthropic/NVIDIA 등 한 회사에 지나치게 편중하지 말 것
- 새 모델/제품, 에이전트, 개발자 도구, 온디바이스 AI/NPU, AI 반도체,
  주요 연구, 정책/규제 등에서 중요한 것을 우선
- Android 온디바이스 AI, NPU, 소형 모델의 큰 이슈가 있으면 가중치를 주되
  억지로 넣지는 말 것
- 1번이 가장 중요한 이슈

[중요]
- candidate_id는 반드시 아래 후보 중 하나를 그대로 사용
- URL이나 출처를 새로 만들지 말 것
- 사실과 해석을 구분하고 과장하지 말 것
- 정보가 부족한 부분은 추측하지 말 것

[뉴스 후보]
{json.dumps(compact, ensure_ascii=False)}

[출력]
설명/마크다운 없이 JSON 객체만 반환:

{{
  "daily_summary": "오늘 AI 업계 흐름을 2문장 이내로 요약",
  "issues": [
    {{
      "candidate_id": 1,
      "category": "OpenAI",
      "title": "한국어 큰 제목",
      "summary": "핵심 1~2문장",
      "detail": "배경과 실제 변화 2~4문장",
      "why_it_matters": "왜 중요한지 1~3문장",
      "published_date": "YYYY-MM-DD 또는 확인 어려우면 날짜 문자열"
    }}
  ]
}}
"""

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
    )

    if not response.text:
        raise RuntimeError("Gemini가 빈 응답을 반환했습니다.")

    return validate_ai_result(extract_json(response.text), candidates)


def fallback_without_ai(candidates: list[dict]) -> dict:
    """
    Gemini 무료 쿼터가 0이거나 일시적으로 막혀도 사이트 자체는 업데이트되도록 하는 안전장치.
    RSS 제목 기준으로 앞의 5개를 표시합니다.
    """
    selected = candidates[:5]
    issues = []

    for a in selected:
        issues.append({
            "category": "AI",
            "title": a["title"],
            "summary": a["summary"][:220] if a["summary"] else "최근 공개된 AI 관련 주요 소식입니다.",
            "detail": (
                "현재 Gemini 요약 API를 사용할 수 없어 RSS에서 수집한 기사 정보를 그대로 표시하고 있습니다. "
                "정확한 내용은 원문을 확인해주세요."
            ),
            "why_it_matters": "오늘의 주요 AI 뉴스 후보로 수집된 기사입니다. 원문을 확인해 영향도를 판단할 수 있습니다.",
            "source_name": a["source"],
            "source_url": a["url"],
            "published_date": a["published"],
        })

    return {
        "daily_summary": (
            "최신 AI 뉴스를 RSS로 자동 수집했습니다. "
            "오늘은 Gemini 요약 API를 사용할 수 없어 자동 선별 요약 없이 표시합니다."
        ),
        "issues": issues,
    }



def fetch_usd_krw():
    """
    Frankfurter 공개 API의 최신 일일 기준환율.
    실시간 체결가가 아니라 중앙은행 기반 일일 참고 환율입니다.
    """
    url = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=KRW"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        payload = r.json()
        rate = payload.get("rates", {}).get("KRW")
        if rate is None:
            raise ValueError("KRW 환율이 응답에 없습니다.")
        return {
            "usd_krw": float(rate),
            "date": str(payload.get("date", "")),
            "source": "Frankfurter"
        }
    except Exception as e:
        print(f"[WARNING] Exchange rate fetch failed: {type(e).__name__}: {e}")
        return None



def fetch_usd_krw_history():
    """
    최근 약 10일을 요청한 뒤 실제 환율 데이터가 존재하는
    마지막 7영업일만 저장합니다.
    """
    end = datetime.now(KST).date()
    start = end - timedelta(days=12)
    url = (
        f"https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}"
        "?base=USD&symbols=KRW"
    )

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        payload = r.json()
        rates = payload.get("rates", {})

        rows = []
        for date_str in sorted(rates.keys()):
            rate = rates.get(date_str, {}).get("KRW")
            if rate is not None:
                rows.append({
                    "date": date_str,
                    "rate": float(rate)
                })

        return rows[-7:]
    except Exception as e:
        print(f"[WARNING] Exchange history fetch failed: {type(e).__name__}: {e}")
        return []



def archive_daily_brief(final: dict):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    date_str = str(final.get("date", "")).strip()
    if not date_str:
        raise ValueError("브리핑 날짜가 없습니다.")

    archive_path = HISTORY_DIR / f"{date_str}.json"
    archive_path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")

    dates = []
    if HISTORY_INDEX.exists():
        try:
            existing = json.loads(HISTORY_INDEX.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("dates"), list):
                dates = [str(x) for x in existing["dates"]]
        except Exception:
            dates = []

    dates = sorted(set(dates + [date_str]))
    HISTORY_INDEX.write_text(
        json.dumps({"dates": dates}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"Archived history: {archive_path}")

def main():
    candidates = collect_articles()
    if len(candidates) < 5:
        raise RuntimeError(f"RSS 기사 수집량이 너무 적습니다: {len(candidates)}개")

    try:
        result = summarize_with_gemini(candidates)
        mode = "RSS + Gemini summary"
    except Exception as e:
        # API 무료 쿼터가 없더라도 workflow 전체를 실패시키지 않음
        print(f"[WARNING] Gemini summary failed: {type(e).__name__}: {e}")
        print("[INFO] Falling back to RSS-only mode.")
        result = fallback_without_ai(candidates)
        mode = "RSS-only fallback"

    now = datetime.now(KST)

    exchange_rate = fetch_usd_krw()
    exchange_rate_history = fetch_usd_krw_history()

    final = {
        "date": now.strftime("%Y-%m-%d"),
        "updated_at": now.strftime("%H:%M KST"),
        "generation_mode": mode,
        "daily_summary": result["daily_summary"],
        "exchange_rate": exchange_rate,
        "exchange_rate_history": exchange_rate_history,
        "issues": result["issues"][:5],
    }

    OUTPUT.write_text(
        json.dumps(final, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    archive_daily_brief(final)

    print(f"Updated {OUTPUT}")
    print(f"Mode: {mode}")
    print(f"Collected candidates: {len(candidates)}")
    print(f"Published issues: {len(final['issues'])}")


if __name__ == "__main__":
    main()
