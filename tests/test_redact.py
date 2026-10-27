import pytest

from gateway.redact import luhn_ok, redact


def test_email_phone_card_idnp_iban():
    text = ("Sunt Ana, ana.popescu@mail.md, tel. 069 123 456, card 4111 1111 1111 1111, "
            "IDNP 2001000123456, IBAN MD24AG000225100013104168.")
    r = redact(text)
    assert "ana.popescu@mail.md" not in r.text and "[EMAIL_1]" in r.text
    assert "069 123 456" not in r.text and "[PHONE_1]" in r.text
    assert "4111 1111 1111 1111" not in r.text and "[CARD_1]" in r.text
    assert "2001000123456" not in r.text and "[IDNP_1]" in r.text
    assert "[IBAN_1]" in r.text
    assert r.count == 5
    assert r.restore(r.text) == text


@pytest.mark.parametrize("phone", ["+373 69 123 456", "+37369123456", "022 21 21 21", "(069) 12-34-56"])
def test_phone_formats(phone):
    assert "[PHONE_1]" in redact(f"sunați la {phone} azi").text


def test_numbers_that_are_not_pii_stay():
    text = "Comanda TN-10013 costă 15999 lei, livrare în 2 zile, 1234 5678 9012 3456 nu e card valid."
    assert redact(text).text == text


def test_same_value_same_placeholder_across_messages():
    first = redact("scrie la ion@x.md")
    second = redact("am scris la ion@x.md și la eva@x.md", existing=first)
    assert second.text == "am scris la [EMAIL_1] și la [EMAIL_2]"


def test_restore_longest_placeholder_first():
    r = redact(" ".join(f"u{i}@x.md" for i in range(1, 12)))
    assert r.restore("[EMAIL_11] și [EMAIL_1]") == "u11@x.md și u1@x.md"


def test_luhn():
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")
    assert not luhn_ok("1234")
