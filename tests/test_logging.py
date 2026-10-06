import json
import logging
from uuid import uuid4

from core.logging import (
    JsonFormatter,
    SecretScrubber,
    fill_reference,
    redact_free_text,
    reset_correlation_id,
    scrub_secrets,
    set_correlation_id,
    short_reference,
)


def test_every_known_secret_field_is_scrubbed() -> None:
    fixture = {
        key: "sensitive-value"
        for key in (
            "api_key",
            "apikey",
            "api_secret",
            "apisecret",
            "private_key",
            "privatekey",
            "secret",
            "password",
            "token",
            "access_token",
            "refresh_token",
            "jwt",
            "signature",
            "authorization",
            "authorization_header",
            "auth_header",
            "account_id",
            "portfolio_id",
            "client_order_id",
            "email",
            "ip_address",
        )
    }
    scrubbed = scrub_secrets({"nested": fixture})["nested"]
    assert all(value == "[REDACTED]" for value in scrubbed.values())


def test_scrubber_handles_text_and_json_correlation() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "Bearer secret-token", (), None)
    assert SecretScrubber().filter(record)
    assert "secret-token" not in record.msg
    token = set_correlation_id(uuid4())
    try:
        output = JsonFormatter().format(
            logging.LogRecord("test", logging.INFO, __file__, 1, "event", (), None)
        )
        assert json.loads(output)["correlation_id"]
    finally:
        reset_correlation_id(token)


def test_scrubbed_records_format_several_arguments_and_redact_them() -> None:
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "startup recovery %s: %s (kill switch %s)",
        ("halted", "token=abc123 leaked", "halted"),
        None,
    )
    assert SecretScrubber().filter(record)

    message = json.loads(JsonFormatter().format(record))["message"]

    assert message == "startup recovery halted: token=[REDACTED] leaked (kill switch halted)"


def test_free_text_keeps_the_reference_and_removes_secrets_addresses_and_identifiers() -> None:
    text = (
        "INC-42  approved\nby ops@example.test; api_key=abc123 Bearer xyz "
        "see https://ntfy.example.test/private-topic operator-token-value "
        "order 3f2b1c4e-9a8d-4f6e-b1c2-0a9e8d7c6b5a"
    )

    redacted = redact_free_text(text, secrets=("operator-token-value", ""))

    assert redacted == (
        "INC-42 approved by [REDACTED]; api_key=[REDACTED] Bearer [REDACTED] "
        "see [REDACTED] [REDACTED] order [REDACTED]"
    )


def test_fill_trade_ids_that_share_a_prefix_get_different_references() -> None:
    # Gemini trade IDs count up from a long shared prefix: on 2026-10-06 the first eight
    # characters made five different fills all read "28401418".
    first, second = "2840141812001", "2840141812002"

    assert short_reference(first) == short_reference(second) == "28401418"
    assert (fill_reference(first), fill_reference(second)) == ("41812001", "41812002")
    # A short ID is shown whole, and two pieces of one simulated order keep their numbers.
    assert fill_reference("tid-1") == "tid-1"
    simulated = "sim-3f2b1c4e-9a8d-4f6e-b1c2-0a9e8d7c6b5a"
    assert (fill_reference(f"{simulated}-1"), fill_reference(f"{simulated}-2")) == (
        "7c6b5a-1",
        "7c6b5a-2",
    )


def test_an_order_reference_is_still_the_first_eight_characters() -> None:
    order_id = uuid4()

    assert short_reference(order_id) == str(order_id)[:8]


def test_free_text_leaves_an_ordinary_reference_alone() -> None:
    text = (
        "Sandbox check left an open order; reconciled clean at 14:05 UTC, owner approved "
        "under the planned-maintenance-window-procedure-for-sandbox-checks runbook."
    )
    assert redact_free_text(text) == text
