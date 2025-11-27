# HTTPS 배포 (NGINX + Let's Encrypt 권장)

아래 방식은 사진에 있는 "방법 B" 흐름과 동일합니다. DuckDNS 같은 도메인을 NGINX가 받아서 443 포트로 TLS를 종료하고, 백엔드 Flask 앱은 HTTP(예: 6500 포트)로 안전하게 프록시합니다. Let's Encrypt 인증서를 사용하므로 iOS·안드로이드·크롬 모두 경고 없이 접속할 수 있습니다.

## 준비 사항
- 도메인: `*.duckdns.org`와 같은 호스트를 발급받아 DNS가 서버 IP를 가리키게 만듭니다.
- 포트 개방: 방화벽/라우터에서 **80, 443**을 허용합니다. (백엔드 Flask는 6500 등 내부 포트를 사용)
- 패키지 설치
  ```bash
  sudo apt update
  sudo apt install -y nginx certbot python3-certbot-nginx
  ```

## 1단계 — Flask 앱을 HTTP 모드로 실행
- `.env`에 리버스 프록시 모드를 켭니다.
  ```bash
  USE_REVERSE_PROXY_SSL=true
  EXTERNAL_HOST=rcbotdns.duckdns.org  # 발급받은 도메인으로 교체
  ```
- `config/ytweb.service` 등 systemd 유닛에서 `app.py`를 6500 포트로 띄웁니다(SSL 옵션 없이). 이미 실행 중이면 재시작합니다.
  ```bash
  sudo systemctl restart ytweb.service
  ```

## 2단계 — NGINX 리버스 프록시 설정
1. 예시 설정 파일을 복사 후 도메인을 수정합니다.
   ```bash
   sudo cp config/nginx_ytbot.conf /etc/nginx/sites-available/ytbot.conf
   sudo sed -i 's/rcbotdns.duckdns.org/내도메인.duckdns.org/g' /etc/nginx/sites-available/ytbot.conf
   ```
2. ACME 챌린지용 디렉터리를 준비하고 심볼릭 링크를 만듭니다.
   ```bash
   sudo mkdir -p /var/www/certbot
   sudo ln -sf /etc/nginx/sites-available/ytbot.conf /etc/nginx/sites-enabled/ytbot.conf
   sudo nginx -t && sudo systemctl reload nginx
   ```

## 3단계 — Let's Encrypt 인증서 발급
- NGINX 플러그인을 사용하면 프록시 설정이 자동으로 TLS로 전환됩니다.
  ```bash
  sudo certbot --nginx -d rcbotdns.duckdns.org \
    --email you@example.com --agree-tos --redirect
  ```
- 성공하면 `/etc/letsencrypt/live/<도메인>/`에 인증서가 저장되고, 80 포트 요청은 자동으로 HTTPS로 리다이렉트됩니다.

## 4단계 — 방화벽과 재시작
- 80/443 포트를 허용합니다.
  ```bash
  sudo ufw allow 80,443/tcp
  ```
- NGINX와 Flask 서비스를 재시작하여 설정을 반영합니다.
  ```bash
  sudo systemctl restart nginx
  sudo systemctl restart ytweb.service
  ```

## 5단계 — 접속 테스트
- 브라우저/모바일에서 `https://rcbotdns.duckdns.org/`에 접속해 인증서 경고 없이 열리는지 확인합니다.
- `systemctl status nginx`, `journalctl -u ytweb.service` 로 로그를 점검하세요.

## 추가 참고
- `certbot`은 systemd 타이머로 자동 갱신됩니다. 강제로 갱신하려면 `sudo certbot renew --dry-run`으로 테스트하세요.
- 자체 서명 인증서 기반의 내장 HTTPS가 필요하면 `.env`에서 `USE_REVERSE_PROXY_SSL`을 비워두고 `SSL_CERT_FILE`/`SSL_KEY_FILE`을 지정하면 됩니다(개발/테스트 용도).
