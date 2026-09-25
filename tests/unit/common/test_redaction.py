from smartdata.common.redaction import SecretRedactor


def test_redacts_values_and_credential_bearing_urls(monkeypatch) -> None:
    monkeypatch.setenv("SALES_DB_PASSWORD", "very-secret-value")
    redactor = SecretRedactor.from_environment()
    message = (
        "failed very-secret-value at "
        "postgresql://readonly:another-password@localhost:5432/sales"
    )

    safe = redactor.text(message)

    assert "very-secret-value" not in safe
    assert "another-password" not in safe
    assert safe.count("<redacted>") == 2
