import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from google import genai

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "news.json"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
KST = ZoneInfo("Asia/Seoul")


def strip_code_fence(text: str) -> str:
    text = text.strip()
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
            raise ValueError("Gemini 응답에서 JSON 객체를 찾지 못했습니다.")
        return json.loads(cleaned[start:end + 1])


def validate(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("응답 최상위 형식은 JSON 객체여야 합니다.")

    issues = data.get("issues")
    if not isinstance(issues, list) or len(issues) < 4:
        raise ValueError("AI 이슈가 4개 이상 필요합니다.")

    # 화면은 최대 5개까지만 표시
    issues = issues[:5]

    required = {
        "category", "title", "summary", "detail",
        "why_it_matters", "source_name", "source_url", "published_date"
    }

    clean_issues = []
    for idx, item in enumerate(issues, 1):
        if not isinstance(item, dict):
            raise ValueError(f"{idx}번 이슈 형식이 올바르지 않습니다.")
        missing = [key for key in required if not str(item.get(key, "")).strip()]
        if missing:
            raise ValueError(f"{idx}번 이슈에 누락된 필드: {', '.join(missing)}")
        clean_issues.append({key: str(item[key]).strip() for key in required})

    today = datetime.now(KST)
    return {
        "date": today.strftime("%Y-%m-%d"),
        "updated_at": today.strftime("%H:%M KST"),
        "daily_summary": str(data.get("daily_summary", "")).strip(),
        "issues": clean_issues,
    }


def main():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 환경변수가 없습니다.")

    now = datetime.now(KST)
    date_text = now.strftime("%Y-%m-%d %H:%M KST")

    prompt = f"""
현재 시각은 {date_text}이다.

Google Search를 사용해 '지금 알아둘 가치가 있는 AI 업계 뉴스'를 조사한 뒤,
한국어 AI 데일리 브리핑을 만들어라.

[목표]
- 최근 24~36시간에 실제로 발표되거나 보도된 AI 관련 이슈를 우선한다.
- 정말 중요한 이슈가 부족하면 최근 72시간까지 확장해도 된다.
- 총 5개를 선정한다.
- 같은 사건을 여러 매체가 다룬 경우 하나로 합친다.
- 단순 루머, 광고성 글, SEO성 재가공 기사, 출처 불명 정보는 제외한다.
- 가능하면 공식 발표/공식 블로그/기업 문서/신뢰도 높은 주요 언론을 우선한다.
- OpenAI, Google/DeepMind, Anthropic, Meta, Microsoft, NVIDIA 등 특정 기업에 편중되지 않게 한다.
- 새 모델/제품, AI 에이전트, 온디바이스 AI/NPU, AI 반도체, 개발자 생태계,
  주요 연구 성과, 정책/규제, 산업적으로 큰 사건을 골고루 고려한다.
- 특히 Android 온디바이스 AI, 스마트폰 NPU, 소형 모델 관련 큰 이슈가 있다면 중요도를 높인다.
- 단, 중요하지 않은 소식을 억지로 넣지는 않는다.

[작성 방식]
- 제목: 한국어로 짧고 명확하게.
- summary: 핵심을 1~2문장.
- detail: 배경과 실제 변화가 무엇인지 2~4문장.
- why_it_matters: 일반 사용자/개발자/AI 산업 관점에서 왜 중요한지 1~3문장.
- 과장하지 말고 사실과 해석을 구분한다.
- 날짜는 원문 공개일 기준 YYYY-MM-DD.
- source_url은 해당 사실을 직접 확인할 수 있는 실제 웹페이지 URL을 넣는다.
- 1번 이슈가 오늘 가장 중요한 이슈가 되도록 중요도 순으로 정렬한다.

[출력]
설명이나 Markdown 없이 아래 구조의 유효한 JSON 객체만 출력하라.

{{
  "daily_summary": "오늘 AI 업계 전체 흐름을 2문장 이내로 요약",
  "issues": [
    {{
      "category": "OpenAI",
      "title": "큰 제목",
      "summary": "핵심 요약",
      "detail": "상세 설명",
      "why_it_matters": "왜 중요한가",
      "source_name": "출처명",
      "source_url": "https://...",
      "published_date": "YYYY-MM-DD"
    }}
  ]
}}
"""

    client = genai.Client(api_key=api_key)
    interaction = client.interactions.create(
        model=MODEL,
        input=prompt,
        tools=[{"type": "google_search"}],
    )

    raw = interaction.output_text
    if not raw:
        raise RuntimeError("Gemini가 빈 응답을 반환했습니다.")

    data = validate(extract_json(raw))

    OUTPUT.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print(f"Updated: {OUTPUT}")
    print(f"Issues: {len(data['issues'])}")


if __name__ == "__main__":
    main()
