import pytest

from smartdata.profiling.documents.filesystem import safe_datasource_directory_name


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("acceptance-postgresql", "acceptance-postgresql"),
        ("../unsafe/name\\..", "unsafe-name"),
        (" sales / primary ", "sales-primary"),
    ],
)
def test_datasource_directory_name_is_readable_and_safe(
    source: str, expected: str
) -> None:
    result = safe_datasource_directory_name(source)

    assert result == expected
    assert "/" not in result
    assert "\\" not in result
    assert ".." not in result


@pytest.mark.parametrize("source", ["", " ", "..", "../\\"])
def test_datasource_directory_name_rejects_empty_safe_value(source: str) -> None:
    with pytest.raises(ValueError, match="safe directory name"):
        safe_datasource_directory_name(source)
