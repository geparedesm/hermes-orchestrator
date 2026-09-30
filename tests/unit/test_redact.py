import pytest

from ho_core.redact import redact


@pytest.mark.parametrize(
    "text,leak",
    [
        ("export GH=ghp_" + "a" * 36, "ghp_" + "a" * 36),
        ("key sk-ant-" + "b" * 30, "sk-ant-" + "b" * 30),
        ("AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
        ("password=hunter2hunter2hunter2", "hunter2hunter2hunter2"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----", "MIIE"),
    ],
)
def test_credentials_are_redacted(text, leak):
    redacted, changed = redact(text)
    assert changed and leak not in redacted and "[REDACTED]" in redacted


def test_prefix_is_kept_and_plain_text_untouched():
    assert redact("token=abcdefghijklmnop12345")[0] == "token=[REDACTED]"
    assert redact("all 412 tests passed") == ("all 412 tests passed", False)
