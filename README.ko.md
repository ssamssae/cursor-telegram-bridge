# Cursor Telegram Bridge

[English](README.md) · [한국어](README.ko.md) · [Web guide · 웹 설명서](https://product.kangdaejong.com/cursor-telegram-bridge/) · [Web verification](docs/feature-map.md)

텔레그램의 본인 전용 봇으로 컴퓨터의 Cursor에 메시지를 보내고 답변을 받습니다.
브릿지 언어는 한국어·영어 중 고를 수 있습니다. AI 답변의 언어와 모델 설정은 별개입니다.

## 0.4.10 새 기능

`python3 bridge_setup.py doctor`로 설치 상태를 언제든 확인합니다. 진단 명령은 아무것도 바꾸지 않습니다.
`bridge.env`와 토큰 파일, 텔레그램의 토큰 확인(`--offline`이면 건너뜀), `cursor-agent`, tmux 세션과 그 안의 Cursor,
상시 서비스, 리눅스 linger, 다른 터미널에서 도는 Cursor를 봅니다.
줄마다 `[ok]`·`[warn]`·`[fail]`로 표시하고, `[fail]`이 있으면 종료 코드 1로 끝납니다.
설치 도구도 끝에 이 명령을 안내합니다.
같은 이름의 서비스 파일이 있고 설치 도구가 만든 파일이 아니면, 바꾸기 전에 `bridge.env` 옆에 사본을 남깁니다. 대화식 설치는 먼저 묻습니다.
`--dry-run`은 이미 있어서 덮어쓸 파일 목록도 보여 줍니다. [릴리스 노트](RELEASE_NOTES-0.4.10.md)를 참고하세요.

## 0.4.9 새 기능

명령 하나로 제작자와 같은 설정을 합니다: `python3 bridge_setup.py setup --like-author`.
봇 토큰과 본인 채팅 ID를 저장하고, tmux 세션 `cursor` 안에서 Cursor를 띄우고, 상시 서비스를 등록하고,
`cursor-tg` 명령을 만들고, 끝에 두 방향을 한 번씩 확인합니다.
이 설정에서는 Cursor 창에서 직접 친 요청도 폰에서 보낸 요청과 같은 진행 카드와 중지 버튼을 받습니다.
자세한 내용은 아래 절과 [릴리스 노트](RELEASE_NOTES-0.4.9.md)에 있습니다.

## 제작자와 똑같이 쓰기 (한 줄 설치)

BotFather에서 본인 전용 봇을 먼저 만드세요. 그다음 이 한 줄을 실행합니다.

```bash
git clone https://github.com/ssamssae/cursor-telegram-bridge.git && cd cursor-telegram-bridge && python3 bridge_setup.py setup --like-author --language ko
```

사람 손이 필요한 일은 둘뿐입니다. 숨김 입력칸에 토큰을 붙여 넣는 것과, 봇에 `/start`를 보내는 것.
나머지는 자동입니다. 설정 파일 `~/.config/cursor-telegram-bridge/bridge.env`(기존 파일은 백업),
tmux 세션 `cursor`와 그 안의 Cursor 실행, 상시 서비스(macOS launchd, Linux `systemd --user`),
`cursor-tg` 명령, 시험 메시지 1통, 그리고 폰→터미널→폰·터미널→폰 두 방향을 한 번씩 돌려 보는 자가 점검까지 합니다.
Cursor가 폴더 신뢰 같은 질문을 띄우고 있으면 자가 점검은 키를 입력하지 않습니다.
`--like-author` 없이 실행하면 첫 질문으로 `Use the author's setup (recommended)? [Y/n]`을 묻고 기본값은 예입니다.
`n`으로 답하면 공개 기본값을 유지합니다. 제작자 설정이 바꾸는 값은 셋입니다.

| 설정 | 제작자 | 기본값 | 켜지는 것 |
| --- | --- | --- | --- |
| `CUB_TUI_MIRROR_LOCAL` | `1` | `0` | Cursor 창에서 직접 친 요청도 폰으로 옴 |
| `CUB_FLOW_MIRROR` | `1` | 꺼짐 | 진행 카드와 중지 버튼(폰 요청·터미널 요청 모두) |
| `CUB_CONTEXT_CLEAR_PCT` | `0` | `50` | `/clear`를 저절로 보내지 않음 |

그다음부터는 어느 터미널에서든 `cursor-tg`를 치면 브릿지가 쓰는 바로 그 Cursor 세션에 붙습니다. 폰과 터미널이 같은 세션을 씁니다.
이미 붙어 있는 다른 터미널은 떨어지지 않습니다. 따로 연 Cursor는 폰과 연결되지 않으며, 설정 도구가 그런 창을 보면 경고합니다.
`--dry-run`을 붙이면 아무것도 바꾸지 않고 할 일만 보여 줍니다.
`--dry-run`은 이미 있어서 덮어쓸 파일 목록도 보여 줍니다.
같은 이름의 서비스 파일이 있고 설치 도구가 만든 파일이 아니면, 바꾸기 전에 `bridge.env` 옆에 사본을 남깁니다. 대화식 설치는 먼저 묻습니다.
설치가 끝난 뒤에도 `python3 bridge_setup.py doctor`로 언제든 다시 확인할 수 있습니다.
아래 「시작하기」는 같은 일을 손으로 하는 방법입니다.

## 시작하기

```bash
git clone https://github.com/ssamssae/cursor-telegram-bridge.git
cd cursor-telegram-bridge
cp config.example.env .env
```

BotFather에서 본인 전용 봇을 만들고, 안내받은 토큰을 로컬 토큰 파일에 저장하세요.
파일 형식은 `{"api_key": "본인 봇 토큰"}`입니다. 이 파일과 `.env`는 공개 저장소나 채팅에 올리지 마세요.
토큰 파일 경로와 본인 채팅 ID를 `.env`의 `CUB_TOKEN_FILE`, `CUB_CHAT_ID`에 지정합니다.
본인 채팅 ID 확인 절차는 [영문 설치 안내](README.md)를 참고하세요.

```bash
set -a
source .env
set +a
python3 cursor_telegram_bridge.py
```

`Cursor` CLI 설치·로그인과 기존 터미널 세션 연결은 [영문 설치 안내](README.md)를 따릅니다.
공개 실행 파일과 같은 폴더의 공통 Python 파일들도 함께 설치해야 합니다.

## 언어와 기본 사용

봇 대화에서 다음 명령을 보냅니다.

```text
/language ko
/language en
/language
/ping
```

- `/language ko`: 브릿지 안내와 버튼을 한국어로 표시합니다.
- `/language en`: 영어로 표시합니다.
- `/language`: 현재 선택을 확인합니다.
- `/ping`: 브릿지 연결 응답을 확인합니다.

언어 명령은 브릿지가 처리하며 Cursor 입력으로 보내지 않습니다. 선택은 재시작 후에도 유지됩니다.
그다음 평소처럼 요청을 보내면 연결된 Cursor 세션에서 처리하고 완료된 답변을 전달합니다.
승인·선택 카드의 안내는 선택한 언어로 보이고, 질문·선택지·모델 이름·코드는 원문을 유지합니다.

## 설정과 문제 확인

문제가 있으면 내려받은 폴더에서 진단 명령부터 실행하세요.

```bash
python3 bridge_setup.py doctor
```

확인 항목마다 `[ok]`·`[warn]`·`[fail]` 한 줄과 짧은 이유를 보여 줍니다. `[fail]`이 하나라도 있으면 종료 코드 1로 끝납니다. 진단 명령은 아무것도 바꾸지 않습니다.

- `bridge.env`와 토큰 파일이 있는지, 본인 계정만 읽을 수 있는지, 채팅 ID가 있는지 봅니다. 토큰 값은 출력하지 않습니다.
- 텔레그램이 토큰을 받는지 `getMe`로 한 번 확인합니다. `--offline`을 붙이면 건너뜁니다.
- `cursor-agent`를 찾을 수 있는지 봅니다.
- tmux 설치, 브릿지 tmux 세션, 그 세션에서 도는 Cursor를 봅니다. tmux나 세션이 없는 동안에는 메시지마다 숨은 `cursor-agent`가 한 번씩 돕니다. 그래서 경고로 알립니다. `CUB_TUI_FALLBACK_HEADLESS=0`이면 실패로 알립니다.
- 상시 서비스(macOS launchd, 리눅스 `systemd --user`)가 떠 있는지 봅니다. 리눅스에서는 linger도 봅니다. linger가 꺼져 있으면 로그아웃할 때 서비스가 멈춥니다. 켜는 명령(`sudo loginctl enable-linger $USER`)은 안내만 하고 실행하지 않습니다.
- 다른 터미널에서 도는 Cursor는 폰과 연결되지 않습니다. 그런 Cursor가 보이면 경고합니다. `cursor-tg`로 붙으세요.

처음 사용할 언어는 `.env` 또는 서비스 설정의 `CUB_LANGUAGE=ko`로 지정할 수 있습니다.
이미 대화에서 고른 언어가 있으면 그 선택을 우선합니다. 설정을 바꾸려면 `/language`를 사용하세요.
여러 브릿지를 같은 컴퓨터에서 쓰더라도 언어 선택 파일은 브릿지별로 구분됩니다.

응답이 없으면 프로세스 실행 여부, 본인 채팅 ID, 토큰 파일 경로와 연결한 터미널 세션을 확인하세요.
로그를 공유할 때는 토큰과 대화 내용을 제거하세요. 언어 파일 저장에 실패하면 오류를 알리고 기존 언어를 유지합니다.

## 지원 범위

연결·전달·오류 안내, 승인·선택 카드 설명, 복구·확인 버튼과 주요 설정 응답을 번역합니다.
AI 답변과 사용자 입력을 번역 서비스로 보내지 않으며 별도 번역 비용이 없습니다.
터미널 설치·진단 출력, 고급 운영 보고와 CLI가 직접 만든 문구는 기존 언어를 유지합니다.
자세한 검증 경로는 [언어 설정 문서](docs/i18n.md), 고급 설정은 [영문 문서](README.md)에 있습니다.
