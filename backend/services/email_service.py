import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from config import get_settings


def _load_template(filename: str = "survey_invite.html") -> str:
    tpl_path = Path(__file__).parent.parent / "templates" / filename
    return tpl_path.read_text(encoding="utf-8")


def render_email(name: str, org: str, survey_url: str) -> str:
    html = _load_template("survey_invite.html")
    return html.replace("{{name}}", name).replace("{{org}}", org).replace("{{survey_url}}", survey_url)


def render_completion(name: str, org: str, review_url: str) -> str:
    """응답 제출 직후 자동 발송 — 본인용 확인 링크 포함."""
    html = _load_template("survey_complete.html")
    return (html.replace("{{name}}", name)
                .replace("{{org}}", org)
                .replace("{{review_url}}", review_url))


def render_custom(name: str, org: str, survey_url: str, body_html: str) -> str:
    """관리자가 작성한 자유 본문에 동일한 placeholder 치환을 적용한다.
    줄바꿈만 입력한 경우(plain text)도 자동으로 <br>로 변환.
    """
    html = body_html or ""
    # plain text 입력(태그가 거의 없을 때) 줄바꿈 → <br>
    if "<p" not in html and "<br" not in html and "<div" not in html:
        html = html.replace("\r\n", "\n").replace("\n", "<br>")
    html = (html.replace("{{name}}", name)
                .replace("{{org}}", org)
                .replace("{{survey_url}}", survey_url))
    # 기본 본문 wrapper (font/letter-spacing만 통일)
    return (
        '<div style="font-family:\'Noto Sans KR\',sans-serif;'
        'font-size:14px;line-height:1.7;color:#222;max-width:640px;margin:0 auto;'
        'padding:24px">' + html + '</div>'
    )


def send_email(to_email: str, subject: str, html_body: str) -> bool:
    s = get_settings()
    msg = MIMEMultipart("alternative")
    msg["From"] = f"AURI 청사관리실태조사 <{s.GMAIL_USER}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(s.GMAIL_USER, s.GMAIL_APP_PASSWORD)
    server.sendmail(s.GMAIL_USER, to_email, msg.as_string())
    server.quit()
    return True
