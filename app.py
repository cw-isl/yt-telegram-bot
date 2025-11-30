from __future__ import annotations

import os
import ssl
import uuid
from pathlib import Path
from typing import List

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from flask import send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from youtube_recorder_bot import capture_live_frame, load_settings, save_settings, yt_download


BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(env_path: Path = BASE_DIR / ".env") -> None:
    """Load key/value pairs from a .env file if it exists."""

    if not env_path.exists():
        return

    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

app = Flask(__name__)
app.secret_key = "dev-secret"  # replace in production
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


def _bool_env(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _jobs_state():
    return {
        "recording": {"live": 32, "download": 0},
        "transcript": {"active": 72},
        "summary": {"active": 48},
    }


def _ssl_context():
    """Return an SSL context tuple when certificate paths are configured.

    The built-in Flask server is still meant for development only, but this
    helper makes it easy to bind HTTPS to port 6500 when you provide
    SSL_CERT_FILE and SSL_KEY_FILE environment variables.
    """

    cert_path = Path(os.getenv("SSL_CERT_FILE", ""))
    key_path = Path(os.getenv("SSL_KEY_FILE", ""))

    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)
    return None


def _reverse_proxy_enabled() -> bool:
    """Return True when TLS termination is handled by a reverse proxy."""

    return _bool_env("USE_REVERSE_PROXY_SSL")


def _https_status() -> dict:
    """Return certificate visibility hints for the UI."""

    if _reverse_proxy_enabled():
        domain = (
            os.getenv("EXTERNAL_HOST")
            or os.getenv("SERVER_NAME")
            or "설정한 도메인"
        )
        return {
            "active": True,
            "message": "NGINX 리버스 프록시가 Let's Encrypt 인증서를 관리하며 Flask는 HTTP로 동작합니다.",
            "cert_subject": domain,
        }

    cert_path = Path(os.getenv("SSL_CERT_FILE", ""))
    key_path = Path(os.getenv("SSL_KEY_FILE", ""))
    if not (cert_path.exists() and key_path.exists()):
        return {
            "active": False,
            "message": "유효한 인증서 경로가 설정되지 않아 HTTP로 동작 중입니다. 리버스 프록시를 사용한다면 USE_REVERSE_PROXY_SSL 환경 변수를 true로 지정하세요.",
            "cert_subject": None,
        }

    subject = None
    try:
        cert_info = ssl._ssl._test_decode_cert(str(cert_path))
        subject_items = dict(cert_info.get("subject", []))
        cn = subject_items.get("commonName") or subject_items.get("organizationName")
        if cn:
            subject = cn
    except Exception:
        subject = None

    return {
        "active": True,
        "message": "신뢰할 수 있는 인증서가 필요합니다. 발급 기관이 루트 인증서에 포함되어야 경고가 사라집니다.",
        "cert_subject": subject,
    }


def _looks_like_live_url(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in ["youtube.com/live", "youtu.be", "live.youtube.com"])


@app.route("/")
def index():
    settings = load_settings()
    categories: List[dict] = settings.get("ui", {}).get("onedrive_categories", [])
    jobs = _jobs_state()
    return render_template(
        "index.html",
        settings=settings,
        categories=categories,
        jobs=jobs,
        https_state=_https_status(),
    )


@app.route("/record/live", methods=["POST"])
def record_live_action():
    action = request.form.get("action")
    live_url = request.form.get("live_url", "").strip()

    if action == "녹화 시작":
        if not live_url:
            flash("라이브 주소를 입력하세요.", "warning")
            return redirect(url_for("index") + "#live")
        if not _looks_like_live_url(live_url):
            flash("라이브 링크를 넣어주세요. 실시간 스트림 주소를 확인하세요.", "warning")
            return redirect(url_for("index") + "#live")
        flash("라이브 녹화를 시작했습니다.", "success")
    elif action == "종료":
        flash("녹화를 종료했습니다.", "info")
    elif action:
        flash(f"{action} 작업을 시작했습니다.", "info")
    return redirect(url_for("index") + "#live")


@app.route("/capture/live", methods=["POST"])
def capture_live_action():
    payload = request.get_json(silent=True) or {}
    live_url = (payload.get("live_url") or "").strip()
    settings = load_settings()
    capture_dir = Path(settings.get("paths", {}).get("captures") or BASE_DIR / "static" / "captures")

    if not live_url:
        return jsonify({"ok": False, "message": "라이브 주소를 입력한 뒤 캡처하세요."}), 400

    if not _looks_like_live_url(live_url):
        return jsonify({"ok": False, "message": "유튜브 라이브 링크가 맞는지 확인하세요."}), 400

    output_path, error = capture_live_frame(live_url, capture_dir)
    if error or not output_path:
        return jsonify({"ok": False, "message": error or "캡처에 실패했습니다."}), 500

    public_url = url_for("serve_capture", filename=output_path.name)
    return jsonify({"ok": True, "message": f"서버에서 캡처를 완료했습니다: {output_path.name}", "image_url": public_url})


@app.route("/captures/<path:filename>")
def serve_capture(filename: str):
    settings = load_settings()
    capture_dir = Path(settings.get("paths", {}).get("captures") or BASE_DIR / "static" / "captures").expanduser().resolve()
    file_path = (capture_dir / filename).resolve()

    if not str(file_path).startswith(str(capture_dir)) or not file_path.exists():
        abort(404)

    return send_from_directory(capture_dir, file_path.name)


@app.route("/download", methods=["POST"])
def download_action():
    payload = request.get_json(silent=True) or {}
    link = (payload.get("video_url") or request.form.get("video_url") or "").strip()
    upload_after = payload.get("upload_after") in {True, "true", "on", "1", 1}

    if not link:
        return jsonify({"ok": False, "message": "다운로드할 유튜브 링크를 입력하세요."}), 400

    dest = Path(load_settings().get("paths", {}).get("downloads", "downloads"))
    result = yt_download(link, dest)
    if not result:
        return jsonify({"ok": False, "message": "다운로드에 실패했습니다. 링크 또는 ffmpeg 설치를 확인하세요."}), 500

    message = f"다운로드 완료: {result.name}"
    if upload_after:
        # Placeholder for future upload integration
        message += " (업로드 예약)"

    flash(message, "success")
    return jsonify({"ok": True, "message": message, "file_name": result.name})


@app.route("/transcript", methods=["POST"])
def transcript_action():
    file_name = request.form.get("file_name")
    if file_name:
        flash(f"전사 작업을 예약했습니다: {file_name}", "success")
    else:
        flash("파일을 선택하세요.", "warning")
    return redirect(url_for("index"))


@app.route("/summary", methods=["POST"])
def summary_action():
    file_name = request.form.get("file_name")
    if file_name:
        flash(f"요약 작업을 예약했습니다: {file_name}", "success")
    else:
        flash("파일을 선택하세요.", "warning")
    return redirect(url_for("index"))


@app.route("/settings", methods=["POST"])
def settings_action():
    current = load_settings()
    current.setdefault("paths", {})
    current.setdefault("auth", {})

    current["paths"]["recordings"] = request.form.get("recordings", current["paths"].get("recordings"))
    current["paths"]["captures"] = request.form.get("captures", current["paths"].get("captures"))
    current["paths"]["downloads"] = request.form.get("downloads", current["paths"].get("downloads"))
    current["paths"]["transcripts"] = request.form.get("transcripts", current["paths"].get("transcripts"))
    current["paths"]["summaries"] = request.form.get("summaries", current["paths"].get("summaries"))
    current["paths"]["onedrive_upload"] = request.form.get("onedrive_upload", current["paths"].get("onedrive_upload"))
    current["paths"]["transcript_upload"] = request.form.get("transcript_upload", current["paths"].get("transcript_upload"))
    current["paths"]["summary_upload"] = request.form.get("summary_upload", current["paths"].get("summary_upload"))

    current["auth"]["chatgpt_token"] = request.form.get("chatgpt_token", "")
    current["auth"]["onedrive_account"] = request.form.get("onedrive_account", "")

    save_settings(current)
    flash("설정이 저장되었습니다.", "success")
    return redirect(url_for("index"))


@app.context_processor
def inject_nav():
    nav_links = [
        {"href": url_for("index") + "#live", "label": "라이브 녹화"},
        {"href": url_for("index") + "#download-box", "label": "링크 다운로드"},
        {"href": "#transcript", "label": "전사 및 요약"},
        {"href": "#settings", "label": "설정"},
    ]
    return {"nav_links": nav_links}


@app.template_filter("percent_class")
def percent_class(value: int) -> str:
    if value >= 80:
        return "bg-success"
    if value >= 50:
        return "bg-info"
    return "bg-warning"


@app.route("/ideas")
def ideas():
    suggested = [
        "원드라이브 업로드 히스토리 로그",
        "여러 요약 버전(짧게/길게) 병렬 생성",
        "자동 재시도 스케줄링 및 이메일 알림",
    ]
    return {"ideas": suggested, "token": uuid.uuid4().hex}


if __name__ == "__main__":
    proxy_mode = _reverse_proxy_enabled()
    ssl_context = None if proxy_mode else _ssl_context()

    if proxy_mode:
        print(
            "USE_REVERSE_PROXY_SSL=true 로 설정되었습니다. NGINX가 TLS를 종료하고 Flask는 HTTP 6500 포트에서 동작합니다."
        )
        app.run(debug=True, host="0.0.0.0", port=6500)
    elif ssl_context:
        print("Starting HTTPS on port 6500 with provided certificates.")
        app.run(debug=True, host="0.0.0.0", port=6500, ssl_context=ssl_context)
    else:
        print("SSL_CERT_FILE 또는 SSL_KEY_FILE이 설정되지 않아 HTTP로 실행합니다.")
        app.run(debug=True, host="0.0.0.0", port=6500)
