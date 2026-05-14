import base64
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from typing import Literal
import requests

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class SMTPConfig(BaseModel):
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    sender_email: str = ""
    sender_name: str = "Trustnode Edge"
    use_tls: bool = True
    use_ssl: bool = False


class PHPMailConfig(BaseModel):
    endpoint_url: str = ""
    api_token: str = ""
    auth_header: str = "X-API-TOKEN"
    timeout_ms: int = 6000
    verify_tls: bool = True


class EmailAttachment(BaseModel):
    filename: str
    content_b64: str
    content_type: str = "application/octet-stream"


class EmailRequest(BaseModel):
    transport: Literal["smtp", "php_http"] = "smtp"
    smtp: SMTPConfig
    php_mail: PHPMailConfig | None = None
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    subject: str = "Trustnode Notification"
    html_body: str = ""
    text_body: str = ""
    attachments: list[EmailAttachment] = Field(default_factory=list)


class EmailResult(BaseModel):
    ok: bool
    message: str
    recipients: list[str] = Field(default_factory=list)


class TestEmailRequest(BaseModel):
    transport: Literal["smtp", "php_http"] = "smtp"
    smtp: SMTPConfig
    php_mail: PHPMailConfig | None = None
    to: str = ""
    mode: Literal["test", "alarm"] = "test"


def _build_message(payload: EmailRequest) -> MIMEMultipart:
    sender_name = (payload.smtp.sender_name or "Trustnode Edge").strip()
    sender_email = (payload.smtp.sender_email or payload.smtp.username or "").strip()
    # When attachments are present we use a `mixed` outer container with an
    # `alternative` body section, otherwise plain `alternative` (text + html).
    has_attachments = bool(payload.attachments)
    if has_attachments:
        msg = MIMEMultipart("mixed")
        body_part = MIMEMultipart("alternative")
    else:
        msg = MIMEMultipart("alternative")
        body_part = msg
    msg["From"] = f"{sender_name} <{sender_email}>" if sender_email else sender_name
    msg["To"] = ", ".join(payload.to)
    if payload.cc:
        msg["Cc"] = ", ".join(payload.cc)
    msg["Subject"] = payload.subject or "Trustnode Notification"
    text_body = payload.text_body or "Trustnode notification."
    html_body = payload.html_body or f"<html><body><pre>{text_body}</pre></body></html>"
    body_part.attach(MIMEText(text_body, "plain", "utf-8"))
    body_part.attach(MIMEText(html_body, "html", "utf-8"))
    if has_attachments:
        msg.attach(body_part)
        for att in payload.attachments:
            try:
                raw = base64.b64decode(att.content_b64 or "")
            except Exception:
                continue
            ctype = str(att.content_type or "application/octet-stream").strip() or "application/octet-stream"
            main, _, sub = ctype.partition("/")
            part = MIMEBase(main or "application", sub or "octet-stream")
            part.set_payload(raw)
            encoders.encode_base64(part)
            filename = str(att.filename or "attachment.bin").strip() or "attachment.bin"
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            msg.attach(part)
    return msg


def _send_email(payload: EmailRequest) -> EmailResult:
    recipients = [r.strip() for r in [*payload.to, *payload.cc, *payload.bcc] if str(r).strip()]
    if not recipients:
        return EmailResult(ok=False, message="No recipients provided.", recipients=[])
    smtp_cfg = payload.smtp
    host = smtp_cfg.host.strip()
    if not host:
        return EmailResult(ok=False, message="SMTP host is required.", recipients=recipients)
    port = int(smtp_cfg.port or 0) or (465 if smtp_cfg.use_ssl else 587)
    username = smtp_cfg.username.strip()
    password = smtp_cfg.password
    msg = _build_message(payload)
    try:
        if smtp_cfg.use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                if username:
                    server.login(username, password)
                server.sendmail(msg["From"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                if smtp_cfg.use_tls:
                    server.starttls()
                    server.ehlo()
                if username:
                    server.login(username, password)
                server.sendmail(msg["From"], recipients, msg.as_string())
        return EmailResult(ok=True, message=f"Email sent to {len(recipients)} recipient(s).", recipients=recipients)
    except Exception as exc:
        return EmailResult(ok=False, message=f"Email send failed: {exc}", recipients=recipients)


def _send_email_php(payload: EmailRequest) -> EmailResult:
    recipients = [r.strip() for r in [*payload.to, *payload.cc, *payload.bcc] if str(r).strip()]
    if not recipients:
        return EmailResult(ok=False, message="No recipients provided.", recipients=[])
    php = payload.php_mail or PHPMailConfig()
    endpoint = str(php.endpoint_url or "").strip()
    if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
        return EmailResult(ok=False, message="PHP endpoint URL must start with http:// or https://", recipients=recipients)
    timeout_s = max(2.0, min(float(php.timeout_ms or 6000) / 1000.0, 30.0))
    headers = {"Content-Type": "application/json", "User-Agent": "trustnode-edge-notifier"}
    token = str(php.api_token or "").strip()
    auth_header = str(php.auth_header or "X-API-TOKEN").strip() or "X-API-TOKEN"
    if token:
        headers[auth_header] = token
    sender_email = (payload.smtp.sender_email or payload.smtp.username or "").strip()
    sender_name = (payload.smtp.sender_name or "Trustnode Edge").strip()
    # Dolibarr-style payload compatibility.
    # Sends both normalized keys and legacy keys so endpoints can accept either shape.
    is_html = 1 if (payload.html_body or "").strip() else 0
    plain_body = (payload.text_body or "").strip()
    html_body = (payload.html_body or "").strip()
    final_body = html_body if is_html else (plain_body or html_body or "Trustnode notification.")

    attachments_payload = []
    for att in (payload.attachments or []):
        if not att.filename or not att.content_b64:
            continue
        attachments_payload.append({
            "filename": att.filename,
            "content_b64": att.content_b64,
            "content_type": att.content_type or "application/octet-stream",
        })

    body = {
        # Dolibarr/simple PHP style
        "to": recipients[0] if len(recipients) == 1 else ",".join(recipients),
        "cc": ",".join([x.strip() for x in payload.cc if str(x).strip()]),
        "bcc": ",".join([x.strip() for x in payload.bcc if str(x).strip()]),
        "subject": payload.subject or "Trustnode Notification",
        "body": final_body,
        "is_html": is_html,
        "from_name": sender_name,
        "from_email": sender_email,
        # Backward-compatible keys for existing custom endpoints
        "to_list": recipients,
        "cc_list": payload.cc,
        "bcc_list": payload.bcc,
        "html_body": html_body,
        "text_body": plain_body,
        "sender_email": sender_email,
        "sender_name": sender_name,
        # File attachments (base64). PHP receiver decodes `content_b64`.
        "attachments": attachments_payload,
    }
    try:
        resp = requests.post(
            endpoint,
            headers=headers,
            json=body,
            timeout=timeout_s,
            verify=bool(php.verify_tls),
        )
        if int(resp.status_code) not in (200, 201, 202):
            preview = (resp.text or "").strip().replace("\n", " ")[:180]
            return EmailResult(
                ok=False,
                message=f"PHP mail endpoint error HTTP {resp.status_code}{': ' + preview if preview else ''}",
                recipients=recipients,
            )
        ok = True
        msg = f"Email sent via PHP endpoint to {len(recipients)} recipient(s)."
        try:
            parsed = resp.json() if resp.text else {}
            if isinstance(parsed, dict):
                if "success" in parsed:
                    ok = bool(parsed.get("success"))
                if "message" in parsed and str(parsed.get("message")).strip():
                    msg = str(parsed.get("message")).strip()
        except Exception:
            pass
        return EmailResult(ok=ok, message=msg, recipients=recipients)
    except Exception as exc:
        return EmailResult(ok=False, message=f"PHP mail send failed: {exc}", recipients=recipients)


def send_email_request(payload: EmailRequest) -> EmailResult:
    """Programmatic email send for in-process callers (scheduler, etc.).

    Picks the configured transport and returns the same `EmailResult` shape the
    HTTP endpoint produces. Kept as a module-level helper so services can build
    `EmailRequest` directly without going through the REST layer.
    """
    if payload.transport == "php_http":
        return _send_email_php(payload)
    return _send_email(payload)


@router.post("/send", response_model=EmailResult)
def send_email(payload: EmailRequest) -> EmailResult:
    return send_email_request(payload)


@router.post("/test", response_model=EmailResult)
def test_email(payload: TestEmailRequest) -> EmailResult:
    to = payload.to.strip()
    if not to:
        return EmailResult(ok=False, message="Test recipient email is required.", recipients=[])
    body = (
        "<h2>Trustnode Edge - SMTP Test</h2>"
        "<p>This is a test email from Trustnode Edge notifications.</p>"
    )
    if payload.mode == "alarm":
        body = (
            "<h2 style='color:#dc2626'>Trustnode Alarm Notification Test</h2>"
            "<p><b>Gateway:</b> TEST-GW</p>"
            "<p><b>Tag:</b> TEST_TAG</p>"
            "<p><b>Value:</b> 999.0</p>"
            "<p><b>Severity:</b> Critical</p>"
        )
    req = EmailRequest(
        transport=payload.transport,
        smtp=payload.smtp,
        php_mail=payload.php_mail,
        to=[to],
        subject="Trustnode Edge SMTP Test",
        html_body=body,
        text_body="Trustnode Edge SMTP test message.",
    )
    if payload.transport == "php_http":
        return _send_email_php(req)
    return _send_email(req)
