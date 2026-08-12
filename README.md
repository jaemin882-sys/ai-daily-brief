# AI Daily Brief

매일 오전 7:10(한국시간)에 Gemini + Google Search가 최신 AI 이슈를 검색하고,
중요한 5개를 한국어로 요약해 `news.json`을 자동 업데이트하는 정적 웹페이지입니다.

## 들어있는 파일

- `index.html` : 실제 웹 화면
- `update.py` : Gemini가 최신 AI 뉴스를 검색하고 요약하는 코드
- `news.json` : 오늘의 브리핑 데이터
- `.github/workflows/update-news.yml` : 매일 자동 실행
- `requirements.txt` : Python 라이브러리

## 처음 한 번만 설정하면 되는 것

### 1. GitHub에 새 저장소 만들기

GitHub에서 새 Repository를 하나 만듭니다.

예시 이름:

`ai-daily-brief`

Public 저장소가 가장 간단합니다.

### 2. 이 폴더의 모든 파일 업로드

압축을 풀고 폴더 내부의 파일을 저장소 최상단에 그대로 올립니다.

`.github/workflows/update-news.yml`도 반드시 같이 올라가야 합니다.

### 3. Gemini API Key 등록

Gemini API Key는 HTML에 직접 넣으면 안 됩니다.

GitHub 저장소에서:

`Settings → Secrets and variables → Actions → New repository secret`

Name:

`GEMINI_API_KEY`

Value:

본인의 Gemini API Key

를 입력합니다.

### 4. Actions 쓰기 권한 확인

GitHub 저장소에서:

`Settings → Actions → General → Workflow permissions`

가능하다면:

`Read and write permissions`

를 선택합니다.

이 프로젝트의 workflow 자체에도 `contents: write`가 들어 있습니다.

### 5. 처음 한 번 수동 실행

저장소 상단의:

`Actions → Update AI Daily Brief → Run workflow`

를 누릅니다.

성공하면 `news.json`에 오늘 뉴스 5개가 들어갑니다.

### 6. GitHub Pages 켜기

`Settings → Pages`

에서:

- Source: `Deploy from a branch`
- Branch: `main`
- Folder: `/ (root)`

를 선택하고 저장합니다.

잠시 뒤 아래와 비슷한 주소에서 웹페이지를 볼 수 있습니다.

`https://사용자이름.github.io/ai-daily-brief/`

## 자동 업데이트 시간

현재 설정:

매일 오전 **7:10, Asia/Seoul**

파일:

`.github/workflows/update-news.yml`

안의 아래 부분을 바꾸면 됩니다.

```yaml
schedule:
  - cron: "10 7 * * *"
    timezone: "Asia/Seoul"
```

## 수집 기준

Gemini가 Google Search를 이용해 최근 24~36시간을 우선 검색합니다.

중요 이슈가 부족할 경우 최대 72시간까지 보고,
중복 기사나 단순 루머는 제외하도록 프롬프트에 설정되어 있습니다.

관심도가 높게 설정된 분야:

- 주요 AI 모델 및 제품
- AI 에이전트
- 온디바이스 AI
- Android AI
- 스마트폰 NPU
- 소형 AI 모델
- AI 반도체
- 주요 연구
- 정책/규제

## 로컬에서 시험하기

Python 3.10 이상에서:

```bash
pip install -r requirements.txt
```

Windows PowerShell:

```powershell
$env:GEMINI_API_KEY="여기에_API_KEY"
python update.py
```

성공하면 `news.json`이 생성/갱신됩니다.

로컬 웹 확인은:

```bash
python -m http.server 8000
```

그 후 브라우저에서:

`http://localhost:8000`

을 엽니다.

> `index.html`을 파일로 직접 더블클릭하면 브라우저 보안 정책 때문에 `news.json` fetch가 막힐 수 있으므로 로컬 서버 방식이 안전합니다.

## 참고

AI가 뉴스를 요약하기 때문에 중요한 의사결정에 사용할 정보는 반드시 각 카드의 원문 링크를 확인하세요.

Google Search grounding 사용량은 Gemini API 정책/요금제에 따라 비용이 발생할 수 있습니다.
