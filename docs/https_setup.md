# HTTPS 배포 절차 (1~6단계)

아래 순서대로 진행하면 Flask 앱을 6500 포트 HTTPS 모드로 실행할 수 있습니다.

## 1단계 — 인증서와 키 생성
```bash
cd /root/rcbot
openssl req -x509 --newkey rsa:2048 \
  --keyout key.pem --out cert.pem \
  -days 365 \
  -nodes \
  -subj "/CN=localhost"
```
성공 시 `/root/rcbot/` 안에 `cert.pem`, `key.pem`이 생깁니다.

## 2단계 — 환경 변수 등록
`.env` 파일을 사용해 인증서 경로를 지정하는 것을 권장합니다.

`/root/rcbot/.env` (또는 저장소 루트의 `.env`)에 아래 내용을 추가합니다.
```bash
SSL_CERT_FILE=/root/rcbot/cert.pem
SSL_KEY_FILE=/root/rcbot/key.pem
```

## 3단계 — app.py 수정 사항
앱이 `.env` 파일을 직접 읽어 `SSL_CERT_FILE`·`SSL_KEY_FILE` 값을 환경 변수로 주입합니다. 두 파일이 모두 존재하면 HTTPS(포트 6500)로 구동하고, 값이 없으면 HTTP로 동일한 포트에서 실행합니다.

## 4단계 — systemd 서비스 수정
HTTPS 실행을 위한 예시 서비스 파일(`config/ytweb.service`)을 추가했습니다. 실제 서버에서는 파일을 `/etc/systemd/system/ytweb.service`로 복사 후 경로를 환경에 맞게 조정하세요.
```bash
sudo cp config/ytweb.service /etc/systemd/system/ytweb.service
sudo systemctl daemon-reload
sudo systemctl enable --now ytweb.service
```

## 5단계 — 방화벽 허용
서버 방화벽에서 6500 포트를 개방합니다.
```bash
sudo ufw allow 6500/tcp
```

## 6단계 — 접속 테스트
아이폰/브라우저에서 `https://<서버IP>:6500/`에 접속합니다.
- 자체 서명 인증서이므로 경고가 표시될 수 있습니다.
- "신뢰"를 선택하면 HTTPS로 정상 접속됩니다.
