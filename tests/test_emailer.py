import smtplib

import pytest

import emailer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MEETING = {
    "id": "mtg_123",
    "title": "Weekly Sync",
    "date": "2026-07-06",
    "duration_formatted": "42m",
    "speaker_info": {
        "SPEAKER_00": {"display_name": "Alice Smith", "title": "PM", "company": "Acme"},
        "SPEAKER_01": {"name": "Bob Jones", "company": "Globex"},
        "SPEAKER_02": {"name": ""},  # skipped: no usable name
    },
}

SUMMARY = {
    "title": "Weekly Sync",
    "summary": "The team reviewed progress and agreed next steps.",
    "action_items": [
        {"task": "Ship the release", "who": "Alice", "deadline": "2026-07-10", "priority": "high"},
        {"description": "Write docs", "assigned_to": "Bob"},  # legacy field names
    ],
    "decisions": [{"decision": "Adopt X", "context": "cheaper"}],
    "open_questions": [{"question": "Budget?", "asked_by": "Alice", "answered": False}],
    "figures": [{"figure": "$10k", "context": "Q3 budget", "said_by": "Bob"}],
}


# ---------------------------------------------------------------------------
# parse_recipients
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("a@x.com, b@y.com", ["a@x.com", "b@y.com"]),
    ("a@x.com;b@y.com\nc@z.com", ["a@x.com", "b@y.com", "c@z.com"]),
    ("  solo@x.com  ", ["solo@x.com"]),
    ("", []),
    (None, []),
    (["a@x.com", " b@y.com "], ["a@x.com", "b@y.com"]),
])
def test_parse_recipients(value, expected):
    assert emailer.parse_recipients(value) == expected


# ---------------------------------------------------------------------------
# render_summary_email
# ---------------------------------------------------------------------------

def test_render_produces_subject_html_and_text():
    subject, html, text = emailer.render_summary_email(
        MEETING, SUMMARY, MEETING["speaker_info"], text_body="PLAIN-TEXT-BODY"
    )
    assert subject == "Meeting summary: Weekly Sync (2026-07-06)"
    # Participants (both usable names present, empty one skipped)
    assert "Alice Smith" in html and "Bob Jones" in html
    assert "PM, Acme" in html
    # Action items table, incl. legacy field fallbacks (task/description, who/assigned_to)
    assert "Ship the release" in html and "Write docs" in html
    assert "Alice" in html and "Bob" in html
    # Other sections
    assert "Adopt X" in html and "Budget?" in html and "$10k" in html
    # Supplied text body is used verbatim
    assert text == "PLAIN-TEXT-BODY"


def test_render_escapes_html():
    summary = dict(SUMMARY, title="<script>alert(1)</script>", summary="a & b < c")
    subject, html, text = emailer.render_summary_email(MEETING, summary, {}, text_body="t")
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "a &amp; b &lt; c" in html


def test_render_handles_empty_summary():
    subject, html, text = emailer.render_summary_email({"title": "Empty"}, {}, {})
    assert subject.startswith("Meeting summary: Empty")
    assert "<div" in html  # still returns valid markup
    assert isinstance(text, str) and text  # derived plain-text fallback is non-empty


def test_render_derives_text_when_not_supplied():
    _, html, text = emailer.render_summary_email(MEETING, SUMMARY, MEETING["speaker_info"])
    assert "<" not in text  # tags stripped
    assert "Weekly Sync" in text


# ---------------------------------------------------------------------------
# send_email — SMTP transport selection & message construction
# ---------------------------------------------------------------------------

class FakeSMTP:
    """Captures the login + sent message; records which transport class was used."""
    instances = []

    def __init__(self, host, port, context=None, timeout=None):
        self.host = host
        self.port = port
        self.context = context
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent = msg


class FakeSMTP_SSL(FakeSMTP):
    pass


@pytest.fixture
def patched_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP_SSL)
    return FakeSMTP


BASE_SMTP = {
    "host": "smtp.example.com",
    "username": "user@example.com",
    "password": "secret",
    "from_email": "meetings@example.com",
    "from_name": "Meeting Service",
}


def test_send_starttls_on_587(patched_smtp):
    emailer.send_email(dict(BASE_SMTP, port=587), ["a@x.com", "b@y.com"],
                       "Subj", "<p>hi</p>", "hi")
    inst = patched_smtp.instances[-1]
    assert type(inst) is FakeSMTP  # plain SMTP, not SSL
    assert inst.started_tls is True
    assert inst.logged_in == ("user@example.com", "secret")
    assert inst.sent["To"] == "a@x.com, b@y.com"
    assert inst.sent["Subject"] == "Subj"
    assert 'Meeting Service' in inst.sent["From"] and "meetings@example.com" in inst.sent["From"]


def test_send_ssl_on_465(patched_smtp):
    emailer.send_email(dict(BASE_SMTP, port=465), ["a@x.com"], "S", "<p>h</p>", "h")
    inst = patched_smtp.instances[-1]
    assert type(inst) is FakeSMTP_SSL
    assert inst.started_tls is False  # implicit TLS, no starttls


def test_send_ssl_when_secure_flag(patched_smtp):
    emailer.send_email(dict(BASE_SMTP, port=587, secure=True), ["a@x.com"], "S", "<p>h</p>", "h")
    assert type(patched_smtp.instances[-1]) is FakeSMTP_SSL


def test_send_multipart_alternative(patched_smtp):
    emailer.send_email(dict(BASE_SMTP, port=587), ["a@x.com"], "S", "<p>html</p>", "plain")
    msg = patched_smtp.instances[-1].sent
    assert msg.is_multipart()
    types = {p.get_content_type() for p in msg.iter_parts()}
    assert types == {"text/plain", "text/html"}


def test_send_no_login_without_username(patched_smtp):
    emailer.send_email(dict(BASE_SMTP, port=587, username=""), ["a@x.com"], "S", "<p>h</p>", "h")
    assert patched_smtp.instances[-1].logged_in is None


def test_send_reply_to_header(patched_smtp):
    emailer.send_email(dict(BASE_SMTP, port=587, reply_to="reply@x.com"),
                       ["a@x.com"], "S", "<p>h</p>", "h")
    assert patched_smtp.instances[-1].sent["Reply-To"] == "reply@x.com"


def test_send_raises_without_recipients(patched_smtp):
    with pytest.raises(ValueError):
        emailer.send_email(dict(BASE_SMTP, port=587), [], "S", "<p>h</p>", "h")


def test_send_raises_without_host(patched_smtp):
    with pytest.raises(ValueError):
        emailer.send_email(dict(BASE_SMTP, host="", port=587), ["a@x.com"], "S", "<p>h</p>", "h")
