# Flask 개발 서버 HTTPS 설정 (포트 6500)

기본 Flask 서버는 HTTP로 동작하기 때문에 iOS 사파리 등에서 "안전하지 않은 정보를 제출" 경고가 표시될 수 있습니다. 아래 절차로 6500 포트에만 HTTPS를 적용할 수 있습니다.

1. **인증서/키 준비**
   - 자체 서명 인증서를 생성하려면 (예시):
     ```bash
     openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes \
       -subj "/CN=localhost"
     ```
   - 배포 환경에서는 실제 도메인에 맞는 신뢰된 인증서를 사용하세요.

2. **환경 변수 지정** (`.env` 또는 쉘에서 지정)
   ```bash
   export SSL_CERT_FILE=/절대/경로/cert.pem
   export SSL_KEY_FILE=/절대/경로/key.pem
   ```

3. **서버 실행**
   ```bash
   python app.py
   ```
   - 인증서 경로가 올바르면 Flask 개발 서버가 `https://<호스트>:6500` 으로 기동됩니다.
   - 인증서가 없거나 경로가 잘못되면 자동으로 HTTP로 실행되므로 경고가 다시 보일 수 있습니다.

4. **클라이언트 신뢰 처리**
   - 자체 서명 인증서의 경우, 사용하는 디바이스에 인증서를 신뢰하도록 설치해야 브라우저 경고가 제거됩니다.

> 참고: Flask 기본 서버는 개발용이므로 운영 환경에서는 Nginx/Apache 같은 역방향 프록시에 인증서를 올려 HTTPS를 termination 하는 구성이 권장됩니다.
