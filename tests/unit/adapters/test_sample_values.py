from smartdata.adapters.native_values import normalize_sample_row


def test_sample_values_redact_sensitive_fields_and_binary_values() -> None:
    row = normalize_sample_row(
        {
            "customer_id": 7,
            "email": "person@example.com",
            "access_token": "secret-token",
            "payload": b"abc",
        }
    )

    assert row == {
        "customer_id": 7,
        "email": "<redacted>",
        "access_token": "<redacted>",
        "payload": "<binary:3 bytes>",
    }
