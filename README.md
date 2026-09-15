# codex-budget

Codex의 주간 한도를 일일 예산으로 나누고, 경고·초과 시 선택·tmux 상태줄을 제공합니다.

```text
오늘 5.0/20.0%p | 주간 잔여 65% | 경고 25% · 예산 25%
```

macOS · Python 3.11+ · 로그인된 Codex CLI가 필요합니다. 상태줄은 tmux가 필요합니다 (`brew install tmux`). Codex CLI 0.154.0에서 검증했습니다.

## Python으로 설치

```sh
git clone https://github.com/opendev-choi/codex-budget.git
cd codex-budget
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
codex-budget setup
```

Codex를 재시작하고 `/hooks`에서 `codex-budget` 훅 3개를 검토·신뢰하세요. 이후 `codex-budget doctor`로 확인합니다. 훅은 기존 `codex` 실행에도 적용됩니다. 선택 창용 로컬 MCP 서버도 함께 등록됩니다. 창을 지원하지 않는 실행 환경에서는 직접 입력 명령을 사용하세요.

uv 사용자: `uv tool install git+https://github.com/opendev-choi/codex-budget.git` 후 `codex-budget setup`.

## 설정·실행

```sh
# 고정 15%, 한국 공휴일·주말 제외
codex-budget configure --daily-percent 15 --holidays exclude --weekends exclude
# 잔여량을 남은 사용일에 자동 배분 (기본값: 공휴일·주말 포함)
codex-budget configure --daily-percent auto --holidays include --weekends include
# 일일 예산 대비 경고 구간 (off로 경고만 끄기)
codex-budget configure --warn-at 25,50,75,90
codex-budget-run                 # 상태줄과 함께 실행
codex-budget status              # 예산·리셋 시각 확인
```

**선택 창은 `codex -a on-request` 또는 `codex-budget-run -a on-request`로 실행하세요.** `never`·일부 비대화형 환경에서는 폼이 자동 거절되므로 아래 직접 입력 명령을 사용합니다.

**초과하면 Codex 기본 선택 창**에서 `5%p 추가 사용 / 오늘 제한 해제 / 오늘은 쉬기`를 고릅니다. 계속 사용을 선택하면 원래 요청이 이어지고, 취소·쉬기는 요청을 막습니다. 입력 명령 `예산 +5`, `예산 해제`, `예산 쉬기`, `예산 복귀`도 지원합니다.

- 일일 %는 **주간 총한도 대비**입니다. 예산 20%p의 25% 경고는 오늘 5%p 사용 시 발생합니다.
- 공휴일·주말 제외일은 기본 예산이 0입니다. 설정·집계는 `~/.config/codex-budget/`에 저장합니다.
- 한국 시간 자정 기준이며 첫날은 설치 후부터 집계합니다. 60초마다 조회하고, 조회 공백이 자정을 넘으면 증가분을 다음 날짜에 합산합니다.
- 진행 중인 작업·조회 지연으로 예산을 넘길 수 있습니다. 조회 실패 시 새 요청을 막습니다. 임시공휴일은 `configure --extra-holiday YYYY-MM-DD 이름`으로 추가할 수 있습니다.

## AI 에이전트에게 맡기기

**설치 프롬프트**

```text
https://github.com/opendev-choi/codex-budget 를 내 Mac에 설치해줘.
README와 설치 코드를 확인하고 Python 환경을 분리해 설치해.
기존 Codex 훅을 보존하고, 이 도구의 훅을 검토한 뒤 setup --trust-hooks로 등록해줘.
상태줄에 필요한 tmux도 준비하고 doctor로 확인해줘.
```

**설정 프롬프트**

```text
codex-budget을 하루에 주간 한도의 15%, 한국 공휴일·주말 제외로 설정해줘.
일일 예산의 25·50·75·90%에서 경고하고, 초과 시 Codex 기본 선택 창에서 추가 사용·오늘 해제·쉬기를 고르게 해줘.
현재 설정과 상태줄 실행 명령을 알려줘.
```

## 해제·제거

`codex-budget disable`로 제한을 끄거나, `codex-budget uninstall`로 훅·로컬 MCP 서버·백그라운드 조회를 제거하세요. 설정·집계는 보존됩니다. 패키지는 설치한 환경에서 `python -m pip uninstall codex-budget` 또는 `uv tool uninstall codex-budget`으로 제거합니다.
