# apps/notifications/providers.py
"""Provider-neutral transactional email transport.

The application never names a vendor: ``apps.core.emails`` records an
``EmailLog`` row and ``apps.notifications.tasks`` hands it to whichever
provider :func:`get_provider` resolves from settings. Delivery stays
SYNCHRONOUS — a provider either accepts the message inside the originating
request or the row is FAILED. There is no queue, worker or retry anywhere.

Adding a provider means adding one adapter class here and registering it in
``API_PROVIDERS``; nothing else in the codebase changes.

Configuration (environment variables):

    EMAIL_PROVIDER      smtp | django | console | brevo | sendgrid |
                        mailgun | postmark | resend
    DEFAULT_FROM_EMAIL  "Display Name <address@example.com>" (all providers)

    SMTP providers (Gmail, Zoho, Amazon SES SMTP, Brevo SMTP, …) use Django's
    own EMAIL_HOST / EMAIL_PORT / EMAIL_USE_TLS / EMAIL_USE_SSL /
    EMAIL_HOST_USER / EMAIL_HOST_PASSWORD.

    API providers use EMAIL_API_KEY (BREVO_API_KEY is still honoured for
    backwards compatibility) and, where the vendor needs it,
    EMAIL_API_DOMAIN (Mailgun) / EMAIL_API_BASE_URL (region overrides).

SECURITY: API keys, SMTP passwords and tokens are never logged, never
returned in an error message and never written to the EmailLog.

INLINE IMAGES: providers differ. ``supports_inline_images`` tells the caller
whether a ``cid:`` reference will survive; when it will not, the email HTML
falls back to a hosted logo URL (see apps.core.email_assets). Brevo's v3 API
explicitly does not support inline/CID images, and Gmail strips data: URIs —
which is why the receipt logo rendered broken.
"""
import base64
import logging
from email.utils import parseaddr

from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection

logger = logging.getLogger("apps")

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"
POSTMARK_API_URL = "https://api.postmarkapp.com/email"
RESEND_API_URL = "https://api.resend.com/emails"
MAILGUN_API_BASE = "https://api.mailgun.net/v3"


class EmailProviderError(Exception):
    """An email provider refused or could not accept the message."""

    def __init__(self, provider, http_status, message):
        super().__init__(f"{provider} HTTP {http_status}: {message}")
        self.provider = provider
        self.http_status = http_status
        self.safe_message = str(message)[:200]


class EmailConfigurationError(Exception):
    """The configured provider is missing a required setting."""


def _setting(name, default=""):
    return str(getattr(settings, name, "") or default).strip()


def sender_identity():
    """(name, email) parsed from DEFAULT_FROM_EMAIL, with validation.

    Raises ``EmailConfigurationError`` naming the setting when the value has no
    usable address — never silently substitutes a different sender.
    """
    raw = _setting("DEFAULT_FROM_EMAIL")
    # Tolerate a quote-wrapped env value: hosting dashboards keep the quotes.
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1].strip()
    name, email = parseaddr(raw)
    if not email or "@" not in email or "<" in email or ">" in email:
        raise EmailConfigurationError(
            "DEFAULT_FROM_EMAIL is not a valid sender address. Set it to "
            "'Display Name <address@example.com>' using a verified sender."
        )
    return name, email


def _api_key():
    key = _setting("EMAIL_API_KEY") or _setting("BREVO_API_KEY")
    if not key:
        raise EmailConfigurationError(
            "EMAIL_API_KEY is not configured — set it for the selected email provider."
        )
    return key


def _timeout():
    return getattr(settings, "EMAIL_TIMEOUT", 20) or 20


def _post(provider, url, *, json=None, data=None, files=None, headers=None, auth=None):
    """One HTTP POST with uniform, credential-free error translation."""
    import requests

    try:
        response = requests.post(
            url, json=json, data=data, files=files, headers=headers,
            auth=auth, timeout=_timeout(),
        )
    except requests.exceptions.Timeout as exc:
        raise TimeoutError(f"The request to {provider} timed out.") from exc
    except requests.exceptions.RequestException as exc:
        raise ConnectionError(
            f"{provider} could not be reached ({exc.__class__.__name__})."
        ) from exc

    if response.status_code not in (200, 201, 202):
        try:
            body = response.json()
        except ValueError:
            body = None
        message = _error_text(body) or (response.text or "")[:200]
        raise EmailProviderError(provider, response.status_code, message)
    return response


def _error_text(body):
    """Best human-readable error string from a provider's JSON error body."""
    if isinstance(body, dict):
        for key in ("message", "Message", "error", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:200]
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("message") or first)[:200]
            return str(first)[:200]
        if body.get("code"):
            return str(body["code"])[:200]
    if isinstance(body, list) and body:
        return str(body[0])[:200]
    return ""


class BaseProvider:
    """One transactional email transport.

    ``send(message)`` accepts an :class:`OutgoingEmail` and returns the
    provider's message id (empty string when the provider exposes none). Any
    failure raises — the caller records it on the EmailLog.
    """

    name = "base"
    # Whether a cid: reference in the HTML will actually render.
    supports_inline_images = False

    def send(self, message):  # pragma: no cover - interface
        raise NotImplementedError


class OutgoingEmail:
    """Everything a provider needs, with no Django/DB coupling."""

    def __init__(self, *, subject, text_body, html_body, to_email,
                 attachments=None, inline_images=None):
        self.subject = subject
        self.text_body = text_body or ""
        self.html_body = (html_body or "").strip()
        self.to_email = to_email
        # [(filename, bytes, mimetype)]
        self.attachments = attachments or []
        # [(content_id, filename, bytes, mimetype)]
        self.inline_images = inline_images or []


class _InlineImageMessage(EmailMultiAlternatives):
    """An EmailMessage that builds an RFC-correct ``multipart/related`` tree.

    WHY THIS CLASS EXISTS
    ---------------------
    A ``cid:`` reference in the HTML part only renders when the part sits in a
    ``multipart/related`` container. Django has never exposed a public API for
    that: the old approach was to set the undocumented ``mixed_subtype``
    attribute, and Django 6 removed it — ``message()`` now raises
    ``AttributeError`` as soon as anything is attached, which made **every**
    receipt email fail (a 502 on "Send receipt") on any Django ≥ 6 runtime.

    So the container is built explicitly here, once, instead of depending on a
    removed private attribute. The tree is what mail clients expect:

        multipart/mixed            (only when a real attachment exists)
        └── multipart/related
            ├── multipart/alternative
            │   ├── text/plain
            │   └── text/html
            └── image/*            Content-ID: <cid>, inline

    Everything else (headers, the text/HTML alternative, attachments, the
    backend, TLS, SMTP credentials) keeps using Django's own machinery, so this
    works unchanged on Django 5.x and 6.x, on SMTP, console and locmem
    backends, and with no vendor named.
    """

    def __init__(self, *args, inline_images=(), **kwargs):
        self.inline_images = list(inline_images or ())
        super().__init__(*args, **kwargs)

    def message(self, *, policy=None):
        """Return the MIME tree: multipart/mixed → related → alternative."""
        import email.policy
        from email.encoders import encode_base64
        from email.mime.base import MIMEBase
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.utils import formatdate, make_msgid

        from django.conf import settings as django_settings

        policy = policy or email.policy.default
        if not self.inline_images:
            # Nothing to nest: Django's own single-part/alternative builder.
            return super().message(policy=policy)

        charset = self.encoding or django_settings.DEFAULT_CHARSET
        body = self.body or " "

        # multipart/alternative — plain text first (the fallback), HTML second.
        alternative = MIMEMultipart("alternative", policy=policy)
        alternative.attach(MIMEText(body, "plain", charset, policy=policy))
        for content, mimetype in self.alternatives:
            alternative.attach(
                MIMEText(content, mimetype.split("/", 1)[-1], charset, policy=policy)
            )

        # multipart/related — the alternative plus the cid: images it refers to.
        related = MIMEMultipart("related", policy=policy)
        related.attach(alternative)
        for cid, filename, content, mimetype in self.inline_images:
            maintype, _, subtype = mimetype.partition("/")
            part = MIMEBase(maintype or "image", subtype or "png", policy=policy)
            part.set_payload(content)
            encode_base64(part)
            part.add_header("Content-ID", f"<{cid}>")
            part.add_header("Content-Disposition", "inline", filename=filename)
            related.attach(part)

        # multipart/mixed — the related tree plus real attachments (receipt PDF).
        if self.attachments:
            root = MIMEMultipart("mixed", policy=policy)
            root.attach(related)
            for filename, content, mimetype in self.attachments:
                maintype, _, subtype = mimetype.partition("/")
                if isinstance(content, str):
                    content = content.encode(charset, "surrogateescape")
                part = MIMEBase(maintype or "application", subtype or "octet-stream", policy=policy)
                part.set_payload(content)
                encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=filename)
                root.attach(part)
        else:
            root = related

        self._copy_headers_onto(root, make_msgid, formatdate)
        return root

    def _copy_headers_onto(self, msg, make_msgid, formatdate):
        """Recreate the headers Django's own ``message()`` would have set."""
        from django.conf import settings as django_settings

        def clean(value):
            # Header-injection guard: a CR/LF in a header is never legitimate.
            return str(value).replace("\r", " ").replace("\n", " ")

        msg["Subject"] = clean(self.subject)
        msg["From"] = clean(self.extra_headers.get("From", self.from_email))
        for header, values in (("To", self.to), ("Cc", self.cc), ("Reply-To", self.reply_to)):
            values = [clean(value) for value in (values or []) if value]
            if values:
                msg[header] = ", ".join(values)
        extra = {str(key).lower() for key in self.extra_headers}
        if "date" not in extra:
            msg["Date"] = formatdate(
                localtime=bool(getattr(django_settings, "EMAIL_USE_LOCALTIME", False))
            )
        if "message-id" not in extra:
            msg["Message-ID"] = make_msgid()
        for name, value in self.extra_headers.items():
            if str(name).lower() in {"from", "to", "cc", "reply-to", "bcc"}:
                continue
            msg[str(name)] = clean(value)


class DjangoProvider(BaseProvider):
    """Django's configured EMAIL_BACKEND — SMTP, console or locmem.

    This covers EVERY SMTP vendor (Gmail, Zoho, Amazon SES, Brevo SMTP,
    Mailgun SMTP …): they differ only in host/port/credentials, which Django
    already models. Inline CID images work over real MIME (see
    :class:`_InlineImageMessage`, which owns the multipart/related tree so the
    logo inline-renders without depending on a private Django attribute).
    """

    name = "django"
    supports_inline_images = True

    def send(self, message):
        connection = get_connection(fail_silently=False)
        if message.html_body:
            email = _InlineImageMessage(
                subject=message.subject,
                body=message.text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[message.to_email],
                connection=connection,
                inline_images=message.inline_images,
            )
            email.attach_alternative(message.html_body, "text/html")
        else:
            email = EmailMessage(
                subject=message.subject,
                body=message.text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[message.to_email],
                connection=connection,
            )
        for filename, content, mimetype in message.attachments:
            email.attach(filename, content, mimetype)
        if not email.send(fail_silently=False):
            raise RuntimeError("Email backend reported zero messages delivered.")
        return ""  # Django backends expose no provider message id


class BrevoProvider(BaseProvider):
    """Brevo transactional HTTPS API (works where outbound SMTP is blocked).

    Brevo's v3 API does not support inline/CID images, so the HTML must use a
    hosted logo URL instead.
    """

    name = "brevo"
    supports_inline_images = False

    def send(self, message):
        name, email = sender_identity()
        payload = {
            "sender": {"email": email, **({"name": name} if name else {})},
            "to": [{"email": message.to_email}],
            "subject": message.subject,
            "textContent": message.text_body,
        }
        if message.html_body:
            payload["htmlContent"] = message.html_body
        if message.attachments:
            payload["attachment"] = [
                {"name": filename, "content": base64.b64encode(content).decode("ascii")}
                for filename, content, _ in message.attachments
            ]
        response = _post(
            "Brevo", BREVO_API_URL, json=payload,
            headers={
                "api-key": _api_key(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            message_id = str((response.json() or {}).get("messageId") or "")
        except ValueError:
            message_id = ""
        if not message_id:
            raise EmailProviderError(
                "Brevo", response.status_code, "Accepted without a messageId."
            )
        return message_id[:255]


class SendGridProvider(BaseProvider):
    """SendGrid v3 Mail Send API."""

    name = "sendgrid"
    supports_inline_images = True

    def send(self, message):
        name, email = sender_identity()
        content = []
        if message.text_body:
            content.append({"type": "text/plain", "value": message.text_body})
        if message.html_body:
            content.append({"type": "text/html", "value": message.html_body})
        payload = {
            "personalizations": [{"to": [{"email": message.to_email}]}],
            "from": {"email": email, **({"name": name} if name else {})},
            "subject": message.subject,
            "content": content or [{"type": "text/plain", "value": " "}],
        }
        files = [
            {
                "filename": filename,
                "type": mimetype,
                "disposition": "attachment",
                "content": base64.b64encode(data).decode("ascii"),
            }
            for filename, data, mimetype in message.attachments
        ]
        files += [
            {
                "filename": filename,
                "type": mimetype,
                "disposition": "inline",
                "content_id": cid,
                "content": base64.b64encode(data).decode("ascii"),
            }
            for cid, filename, data, mimetype in message.inline_images
        ]
        if files:
            payload["attachments"] = files
        response = _post(
            "SendGrid", SENDGRID_API_URL, json=payload,
            headers={
                "Authorization": f"Bearer {_api_key()}",
                "Content-Type": "application/json",
            },
        )
        return str(response.headers.get("X-Message-Id") or "")[:255]


class MailgunProvider(BaseProvider):
    """Mailgun messages API (multipart form, domain-scoped)."""

    name = "mailgun"
    supports_inline_images = True

    def send(self, message):
        domain = _setting("EMAIL_API_DOMAIN")
        if not domain:
            raise EmailConfigurationError(
                "EMAIL_API_DOMAIN is required for Mailgun (your sending domain)."
            )
        base = _setting("EMAIL_API_BASE_URL") or MAILGUN_API_BASE
        name, email = sender_identity()
        data = {
            "from": f"{name} <{email}>" if name else email,
            "to": message.to_email,
            "subject": message.subject,
            "text": message.text_body,
        }
        if message.html_body:
            data["html"] = message.html_body
        files = [
            ("attachment", (filename, content, mimetype))
            for filename, content, mimetype in message.attachments
        ]
        files += [
            ("inline", (filename, content, mimetype))
            for _cid, filename, content, mimetype in message.inline_images
        ]
        response = _post(
            "Mailgun", f"{base}/{domain}/messages",
            data=data, files=files or None, auth=("api", _api_key()),
        )
        try:
            return str((response.json() or {}).get("id") or "")[:255]
        except ValueError:
            return ""


class PostmarkProvider(BaseProvider):
    """Postmark single-email API."""

    name = "postmark"
    supports_inline_images = True

    def send(self, message):
        name, email = sender_identity()
        payload = {
            "From": f"{name} <{email}>" if name else email,
            "To": message.to_email,
            "Subject": message.subject,
            "TextBody": message.text_body,
            "MessageStream": _setting("EMAIL_API_STREAM") or "outbound",
        }
        if message.html_body:
            payload["HtmlBody"] = message.html_body
        attachments = [
            {
                "Name": filename,
                "Content": base64.b64encode(content).decode("ascii"),
                "ContentType": mimetype,
            }
            for filename, content, mimetype in message.attachments
        ]
        attachments += [
            {
                "Name": filename,
                "Content": base64.b64encode(content).decode("ascii"),
                "ContentType": mimetype,
                "ContentID": f"cid:{cid}",
            }
            for cid, filename, content, mimetype in message.inline_images
        ]
        if attachments:
            payload["Attachments"] = attachments
        response = _post(
            "Postmark", POSTMARK_API_URL, json=payload,
            headers={
                "X-Postmark-Server-Token": _api_key(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            return str((response.json() or {}).get("MessageID") or "")[:255]
        except ValueError:
            return ""


class ResendProvider(BaseProvider):
    """Resend emails API."""

    name = "resend"
    supports_inline_images = False

    def send(self, message):
        name, email = sender_identity()
        payload = {
            "from": f"{name} <{email}>" if name else email,
            "to": [message.to_email],
            "subject": message.subject,
            "text": message.text_body,
        }
        if message.html_body:
            payload["html"] = message.html_body
        if message.attachments:
            payload["attachments"] = [
                {
                    "filename": filename,
                    "content": base64.b64encode(content).decode("ascii"),
                }
                for filename, content, _ in message.attachments
            ]
        response = _post(
            "Resend", RESEND_API_URL, json=payload,
            headers={
                "Authorization": f"Bearer {_api_key()}",
                "Content-Type": "application/json",
            },
        )
        try:
            return str((response.json() or {}).get("id") or "")[:255]
        except ValueError:
            return ""


# Adapters with vendor-specific API contracts. Every SMTP vendor is served by
# DjangoProvider instead and needs no entry here.
API_PROVIDERS = {
    "brevo": BrevoProvider,
    "sendgrid": SendGridProvider,
    "mailgun": MailgunProvider,
    "postmark": PostmarkProvider,
    "resend": ResendProvider,
}

SMTP_ALIASES = ("django", "smtp", "console", "locmem")


def active_provider_name() -> str:
    """Which provider a delivery attempt will use.

    Deterministic: an explicit EMAIL_PROVIDER always wins. With none set, an
    API key selects Brevo (the historical default) and otherwise Django's
    configured backend is used.
    """
    provider = _setting("EMAIL_PROVIDER").lower()
    if provider in API_PROVIDERS:
        return provider
    if provider in SMTP_ALIASES:
        return "django"
    if _setting("EMAIL_API_KEY") or _setting("BREVO_API_KEY"):
        return "brevo"
    return "django"


def get_provider(name=None) -> BaseProvider:
    """Instantiate the configured provider adapter."""
    name = name or active_provider_name()
    return API_PROVIDERS.get(name, DjangoProvider)()
