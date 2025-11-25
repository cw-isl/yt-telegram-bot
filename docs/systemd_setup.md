# systemd 서비스 설정 가이드

재부팅 후에도 Flask 웹 서버(`app.py`)와 보조 모듈 실행용 스크립트(`youtube_recorder_bot.py`)가 자동으로 재시작되도록 하는 `systemd` 서비스 예시입니다. 두 서비스를 분리해두면 개별적으로 상태 확인·재기동이 쉬워집니다.

## 전제 조건
- Python 가상환경 경로 예시: `/opt/ytbot/.venv`
- 프로젝트 경로 예시: `/opt/ytbot`
- 서비스 실행 사용자: `ytbot`
- Flask 서버는 `app.py`를 통해 `0.0.0.0:5000`에서 실행한다고 가정합니다.

경로와 사용자 이름은 환경에 맞게 수정하세요.

## 서비스 유닛 예시
### 웹 서버: `ytbot-web.service`
```ini
[Unit]
Description=YouTube Recorder Bot Flask Web App
After=network.target

[Service]
User=ytbot
Group=ytbot
WorkingDirectory=/opt/ytbot
Environment="PATH=/opt/ytbot/.venv/bin"
ExecStart=/opt/ytbot/.venv/bin/python app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 보조 스크립트(필요 시): `ytbot-helper.service`
`youtube_recorder_bot.py`를 주기적으로 별도 프로세스로 돌리고 싶을 때 사용합니다. 웹앱 내부에서만 호출한다면 생략 가능합니다.
```ini
[Unit]
Description=YouTube Recorder Bot helper
After=network.target

[Service]
User=ytbot
Group=ytbot
WorkingDirectory=/opt/ytbot
Environment="PATH=/opt/ytbot/.venv/bin"
ExecStart=/opt/ytbot/.venv/bin/python youtube_recorder_bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 설치 및 자동시작 설정
1. 유닛 파일 저장: 위 내용을 각각 `/etc/systemd/system/ytbot-web.service`, `/etc/systemd/system/ytbot-helper.service`에 저장합니다(`sudo` 필요).
2. 데몬 리로드: `sudo systemctl daemon-reload`
3. 부팅 시 자동 시작: `sudo systemctl enable ytbot-web.service` (필요하면 `ytbot-helper.service`도 enable)
4. 즉시 시작: `sudo systemctl start ytbot-web.service`
5. 상태 확인: `sudo systemctl status ytbot-web.service`

## 로그 확인
- 최신 로그: `journalctl -u ytbot-web.service -f`
- Helper 서비스 로그: `journalctl -u ytbot-helper.service -f`

## 재기동/중지 명령
- 재시작: `sudo systemctl restart ytbot-web.service`
- 중지: `sudo systemctl stop ytbot-web.service`

## 참고 사항
- Flask 개발 서버 대신 Gunicorn 등을 사용한다면 `ExecStart`만 교체하면 됩니다.
- `Restart=on-failure` 덕분에 프로세스가 종료되면 자동 재시작됩니다. 특별한 정책이 필요하면 `Restart=` 값을 조정하세요.
```
