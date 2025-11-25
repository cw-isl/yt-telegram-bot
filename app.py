from __future__ import annotations

import uuid
from pathlib import Path
from typing import List

from flask import Flask, flash, redirect, render_template, request, url_for

from youtube_recorder_bot import load_settings, save_settings, yt_download

app = Flask(__name__)
app.secret_key = "dev-secret"  # replace in production


def _jobs_state():
    return {
        "recording": {"live": 32, "download": 0},
        "transcript": {"active": 72},
        "summary": {"active": 48},
    }


@app.route("/")
def index():
    settings = load_settings()
    categories: List[dict] = settings.get("ui", {}).get("onedrive_categories", [])
    jobs = _jobs_state()
    return render_template("index.html", settings=settings, categories=categories, jobs=jobs)


@app.route("/record", methods=["POST"])
def record_action():
    action = request.form.get("action")
    link = request.form.get("video_url") or ""
    if action == "download" and link:
        dest = Path(load_settings().get("paths", {}).get("downloads", "downloads"))
        result = yt_download(link, dest)
        if result:
            flash(f"다운로드 완료: {result.name}", "success")
        else:
            flash("다운로드에 실패했습니다. 링크를 확인하거나 ffmpeg 설치를 점검하세요.", "danger")
    elif action:
        flash(f"{action} 작업을 시작했습니다.", "info")
    return redirect(url_for("index"))


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
        {"href": url_for("index"), "label": "유튜브 영상녹화"},
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
    app.run(debug=True, host="0.0.0.0", port=6500)
