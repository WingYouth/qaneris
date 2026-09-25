from smartdata.adapters.time_series.influxdb import InfluxDBAdapter
from smartdata.contracts import DatasetInfo


def test_connection_uses_authenticated_bucket_read() -> None:
    adapter = InfluxDBAdapter(
        "ds_influxdb",
        {
            "url": "http://127.0.0.1:8086",
            "token": "token",
            "org": "retail-org",
            "bucket": "retail-metrics",
        },
    )
    calls: list[str] = []

    def fake_measurements() -> list[str]:
        calls.append("measurements")
        return ["sales_metrics"]

    adapter._measurements = fake_measurements  # type: ignore[method-assign]

    adapter.test_connection()

    assert calls == ["measurements"]


def test_connection_propagates_authenticated_read_failure() -> None:
    adapter = InfluxDBAdapter(
        "ds_influxdb",
        {
            "url": "http://127.0.0.1:8086",
            "token": "token",
            "org": "retail-org",
            "bucket": "retail-metrics",
        },
    )

    def failed_measurements() -> list[str]:
        raise PermissionError("InfluxDB query failed")

    adapter._measurements = failed_measurements  # type: ignore[method-assign]

    try:
        adapter.test_connection()
    except PermissionError as error:
        assert str(error) == "InfluxDB query failed"
    else:
        raise AssertionError("authenticated read failure was not propagated")


def test_scan_samples_bounds_flux_query_to_three_rows() -> None:
    adapter = InfluxDBAdapter(
        "ds_influxdb",
        {
            "url": "http://127.0.0.1:8086",
            "token": "token",
            "org": "retail-org",
            "bucket": "retail-metrics",
        },
    )
    queries: list[str] = []

    def fake_query(query: str) -> list[dict[str, object]]:
        queries.append(query)
        return [{"_measurement": "sales_metrics", "_value": index} for index in range(10)]

    adapter._query = fake_query  # type: ignore[method-assign]

    samples = adapter.scan_samples(
        [DatasetInfo(datasource_id="ds_influxdb", name="sales_metrics")],
        limit=99,
    )

    assert "|> limit(n: 3)" in queries[0]
    assert len(samples[0].rows) == 3


def test_scan_metadata_discovers_measurement_fields_from_readable_data() -> None:
    adapter = InfluxDBAdapter(
        "ds_influxdb",
        {
            "url": "http://127.0.0.1:8086",
            "token": "token",
            "org": "retail-org",
            "bucket": "retail-metrics",
        },
    )
    queries: list[str] = []

    def fake_query(query: str) -> list[dict[str, object]]:
        queries.append(query)
        if "schema.measurements" in query:
            return [{"_value": "sales_metrics"}]
        return [{"_value": "order_count"}, {"_value": "sales_amount"}]

    adapter._query = fake_query  # type: ignore[method-assign]

    datasets = adapter.scan_metadata()

    assert [field.name for field in datasets[0].fields] == ["order_count", "sales_amount"]
    assert "|> distinct(column: \"_field\")" in queries[1]
