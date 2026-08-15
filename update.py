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
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

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

    raw_terms = data.get("ai_terms", [])
    clean_terms = []

    if isinstance(raw_terms, list):
        for item in raw_terms[:3]:
            if not isinstance(item, dict):
                continue
            term = {
                "term": str(item.get("term", "")).strip(),
                "full_name": str(item.get("full_name", "")).strip(),
                "korean": str(item.get("korean", "")).strip(),
                "explanation": str(item.get("explanation", "")).strip(),
                "example": str(item.get("example", "")).strip(),
            }
            if term["term"] and term["explanation"]:
                clean_terms.append(term)

    if len(clean_terms) < 3:
        raise ValueError("AI 단어가 3개 미만입니다.")

    return {
        "daily_summary": str(data.get("daily_summary", "")).strip(),
        "issues": output[:5],
        "ai_terms": clean_terms[:3],
    }



def collect_previous_ai_terms(limit: int = 180) -> list[str]:
    """history에서 이미 배운 AI 용어를 읽어 중복을 줄입니다."""
    terms = []
    seen = set()

    if not HISTORY_DIR.exists():
        return terms

    for path in sorted(HISTORY_DIR.glob("*.json"), reverse=True):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("ai_terms", []):
                term = str(item.get("term", "")).strip()
                key = term.lower()
                if term and key not in seen:
                    seen.add(key)
                    terms.append(term)
                    if len(terms) >= limit:
                        return terms
        except Exception:
            continue

    return terms

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
    previous_terms = collect_previous_ai_terms()
    previous_terms_text = ", ".join(previous_terms) if previous_terms else "없음"

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

[오늘의 AI 단어]
- AI를 처음 배우는 사람을 위한 기초 용어를 정확히 3개 선정한다.
- 처음에는 LLM, Token, Parameter, Prompt, Context Window, Transformer, Inference,
  Fine-tuning, Embedding, RAG, Agent, NPU, Quantization 같은 자주 듣는 기초 개념을 우선한다.
- 뉴스에 나온 단어와 꼭 연결할 필요는 없다. AI 기사나 제품 설명을 이해하는 데 유용한 순서로 고른다.
- 이미 배운 단어는 가능하면 반복하지 않는다.
- 지금까지 배운 단어: {previous_terms_text}
- term: 실제 용어 또는 약자. 예: LLM
- full_name: 약자의 영어 풀네임. 약자가 아니면 대표 영문명.
- korean: 자연스러운 한국어 뜻.
- explanation: AI 문외한도 이해할 수 있게 2~3문장으로 쉽게 설명한다.
- explanation 안에서 또 다른 어려운 AI 용어를 설명 없이 남발하지 않는다.
- example: ChatGPT, 스마트폰, 앱 같은 일상적인 상황으로 한 문장 예시를 든다.
- 약자와 풀네임은 반드시 정확해야 한다.

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
  ],
  "ai_terms": [
    {{
      "term": "LLM",
      "full_name": "Large Language Model",
      "korean": "대규모 언어 모델",
      "explanation": "사람이 쓰는 언어를 이해하고 생성하도록 아주 많은 텍스트로 학습한 AI 모델입니다.",
      "example": "ChatGPT가 질문을 읽고 답을 만드는 데 LLM이 핵심 역할을 합니다."
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
        print(f"[WARNING] Gemini summary failed: {type(e).__name__}: {e}")
        print("[INFO] Keeping the last successful Korean brief. news.json/history will NOT be overwritten.")

        # 환율만 최신화할 수 있으면 기존 news.json 안의 환율 정보만 갱신한다.
        # AI 뉴스 본문/날짜/히스토리는 마지막 정상 한국어 브리핑을 그대로 유지한다.
        if OUTPUT.exists():
            try:
                existing = json.loads(OUTPUT.read_text(encoding="utf-8"))

                exchange_rate = fetch_usd_krw()
                exchange_rate_history = fetch_usd_krw_history()

                if exchange_rate:
                    existing["exchange_rate"] = exchange_rate
                if exchange_rate_history:
                    existing["exchange_rate_history"] = exchange_rate_history

                existing["update_status"] = {
                    "ok": False,
                    "message": "오늘 AI 요약 업데이트에 실패해 마지막 정상 한국어 브리핑을 표시하고 있습니다.",
                    "failed_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
                }

                OUTPUT.write_text(
                    json.dumps(existing, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                print("[INFO] Updated exchange rate only; preserved the last successful AI brief.")
                return
            except Exception as preserve_error:
                print(f"[WARNING] Could not preserve existing news.json: {type(preserve_error).__name__}: {preserve_error}")

        # 기존 정상 브리핑도 없다면 실패 처리해서 잘못된 영어 fallback을 만들지 않는다.
        raise RuntimeError("Gemini 요약에 실패했고 보존할 기존 한국어 브리핑도 없습니다.") from e

    now = datetime.now(KST)

    exchange_rate = fetch_usd_krw()
    exchange_rate_history = fetch_usd_krw_history()

    final = {
        "date": now.strftime("%Y-%m-%d"),
        "updated_at": now.strftime("%H:%M KST"),
        "generation_mode": mode,
        "update_status": {
            "ok": True,
            "message": "오늘 브리핑이 정상 업데이트되었습니다."
        },
        "daily_summary": result["daily_summary"],
        "ai_terms": result.get("ai_terms", []),
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
