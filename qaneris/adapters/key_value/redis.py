"""Redis key-value adapter."""

from __future__ import annotations

from typing import Any, ClassVar

from qaneris.adapters.base import DataSourceAdapter
from qaneris.adapters.native_query import parse_query_payload
from qaneris.adapters.native_values import normalize_sample_value
from qaneris.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult


class RedisAdapter(DataSourceAdapter):
    READ_COMMANDS: ClassVar[set[str]] = {
        "EXISTS",
        "GET",
        "HGET",
        "HGETALL",
        "LRANGE",
        "MGET",
        "SCAN",
        "SCARD",
        "SMEMBERS",
        "TTL",
        "TYPE",
        "ZCARD",
        "ZRANGE",
    }

    #: TLS options redis-py accepts from the materializer, forwarded verbatim in both branches.
    TLS_OPTIONS: ClassVar[tuple[str, ...]] = (
        "ssl_ca_certs",
        "ssl_certfile",
        "ssl_keyfile",
        "ssl_cert_reqs",
        "ssl_check_hostname",
        "ssl_min_version",
    )

    def _tls_options(self) -> dict[str, Any]:
        return {
            name: self.connection[name]
            for name in self.TLS_OPTIONS
            if self.connection.get(name) is not None
        }

    def _client(self):
        try:
            import redis
        except ImportError as error:
            raise RuntimeError("Redis support requires qaneris-platform[redis]") from error
        # TLS parameters must reach redis-py on both branches: a profile that carries a URL still
        # needs its CA, client pair and verification settings. The materializer normalizes the URL
        # scheme to rediss:// when TLS is on, so the URL branch is not quietly plaintext.
        url = self.connection.get("url")
        if url:
            return redis.Redis.from_url(url, decode_responses=True, **self._tls_options())
        return redis.Redis(
            host=self.connection.get("host", "localhost"),
            port=int(self.connection.get("port", 6379)),
            db=int(self.connection.get("database", 0)),
            username=self.connection.get("username"),
            password=self.connection.get("password"),
            ssl=bool(self.connection.get("ssl", False)),
            decode_responses=True,
            **self._tls_options(),
        )

    def test_connection(self) -> None:
        if not self._client().ping():
            raise ConnectionError("Redis ping failed")

    def _keys_by_type(self, maximum: int = 10_000) -> dict[str, list[str]]:
        client = self._client()
        grouped: dict[str, list[str]] = {}
        for index, key in enumerate(client.scan_iter(count=200)):
            if index >= maximum:
                break
            kind = str(client.type(key))
            grouped.setdefault(kind, []).append(str(key))
        return grouped

    def scan_metadata(self) -> list[DatasetInfo]:
        return [
            DatasetInfo(
                datasource_id=self.datasource_id,
                name=f"redis:{kind}",
                kind=kind,
                # Adapter virtual fields describe read capabilities, not native Redis columns.
                fields=[FieldInfo(name=name, data_type=data_type, nullable=False)
                        for name, data_type in (
                            ("key", "string"), ("value", "object"), ("type", "string"),
                            ("ttl", "integer"), ("exists", "boolean"),
                            ("cardinality", "integer"),
                        )],
            )
            for kind in sorted(self._keys_by_type())
        ]

    def _read_value(self, key: str, kind: str) -> Any:
        client = self._client()
        readers = {
            "string": lambda: client.get(key),
            "hash": lambda: client.hgetall(key),
            "list": lambda: client.lrange(key, 0, 19),
            "set": lambda: sorted(client.smembers(key))[:20],
            "zset": lambda: client.zrange(key, 0, 19, withscores=True),
            "stream": lambda: client.xrange(key, count=20),
        }
        return normalize_sample_value(readers.get(kind, lambda: f"<{kind}>")())

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded = max(0, min(limit, 3))
        grouped = self._keys_by_type()
        return [
            DatasetSample(
                datasource_id=self.datasource_id,
                dataset=dataset.name,
                rows=[
                    {"key": key, "value": self._read_value(key, dataset.kind)}
                    for key in grouped.get(dataset.kind, [])[:bounded]
                ],
            )
            for dataset in datasets
        ]

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded = max(0, min(limit, 20))
        grouped = self._keys_by_type()
        return {
            dataset.name: [
                {"key": key, "value": self._read_value(key, dataset.kind)}
                for key in grouped.get(dataset.kind, [])[:bounded]
            ]
            for dataset in datasets
        }

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        payload = parse_query_payload(query)
        command = str(payload.get("command", "")).upper()
        if command not in self.READ_COMMANDS:
            raise ValueError(f"Redis command is not allowed: {command}")
        args = payload.get("args", [])
        if not isinstance(args, list):
            raise TypeError("Redis query args must be a list")
        value = self._client().execute_command(command, *args)
        rows = [{"value": normalize_sample_value(value)}]
        return NormalizedResult(
            source=self.datasource_id,
            dataset="redis",
            columns=["value"],
            rows=rows,
            row_count=1,
        )

    def execute_native(
        self, command: str | dict[str, Any], parameters: tuple[Any, ...] = (),
        max_rows: int = 200,
    ) -> NormalizedResult:
        if not isinstance(command, dict) or parameters or max_rows < 1:
            raise ValueError("Redis formal execution requires a typed bounded command")
        op, args = command.get("command"), command.get("args")
        if op not in self.READ_COMMANDS or not isinstance(args, list):
            raise ValueError("Redis command is not allowed")
        client = self._client()
        if op == "SCAN":
            if set(command) != {"command", "args", "kind", "limit"} or args:
                raise ValueError("Redis SCAN shape is invalid")
            kind, limit = command["kind"], command["limit"]
            if kind not in {"string", "hash", "list", "set", "zset"} or not isinstance(limit, int) or not 1 <= limit <= 1000:
                raise ValueError("Redis SCAN budget is invalid")
            rows: list[dict[str, Any]] = []
            cursor = 0
            # Bound both returned rows and inspected keys, including sparse type scopes.
            inspections = 0
            maximum = min(limit, max_rows) * 20
            pages = 0
            overflow = False
            while inspections < maximum and pages < maximum:
                cursor, keys = client.scan(cursor=cursor, count=min(100, maximum - inspections))
                pages += 1
                for index, key in enumerate(keys):
                    inspections += 1
                    if client.type(key) == kind:
                        rows.append({"key": normalize_sample_value(key)})
                        if len(rows) >= min(limit, max_rows):
                            overflow = index < len(keys) - 1
                            break
                    if inspections >= maximum:
                        break
                if len(rows) >= min(limit, max_rows) or cursor == 0:
                    break
            return NormalizedResult(source=self.datasource_id, dataset="redis",
                                    columns=["key"], rows=rows, row_count=len(rows),
                                    truncated=overflow or cursor != 0 or inspections >= maximum or pages >= maximum)
        if set(command) != {"command", "args"}:
            raise ValueError("Redis command shape is invalid")
        if op in {"LRANGE", "ZRANGE"}:
            if len(args) != 3 or args[1] != 0 or not isinstance(args[2], int) or args[2] < 0 or args[2] >= 1000:
                raise ValueError("Redis range is unbounded")
        elif op == "MGET":
            if not args or len(args) > 1000:
                raise ValueError("Redis MGET is unbounded")
        elif len(args) != 1:
            raise ValueError("Redis exact-key command requires one key")
        if op == "ZRANGE":
            value = client.zrange(args[0], args[1], min(args[2], max_rows), withscores=True)
        elif op == "LRANGE":
            value = client.lrange(args[0], args[1], min(args[2], max_rows))
        else:
            value = getattr(client, op.lower())(*args)
        if op == "MGET":
            rows = [{"key": key, "value": normalize_sample_value(item)}
                    for key, item in zip(args, value, strict=True)]
            columns = ["key", "value"]
        elif op == "HGETALL":
            rows = [{"member": key, "value": normalize_sample_value(item)}
                    for key, item in sorted(value.items())[:max_rows + 1]]
            columns = ["member", "value"]
        elif op in {"LRANGE", "SMEMBERS", "ZRANGE"}:
            values = sorted(value) if op == "SMEMBERS" else value
            rows = [{"value": normalize_sample_value(item)} for item in list(values)[:max_rows + 1]]
            columns = ["value"]
        else:
            rows = [{"value": normalize_sample_value(value)}]
            columns = ["value"]
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return NormalizedResult(source=self.datasource_id, dataset="redis",
                                columns=columns,
                                rows=rows, row_count=len(rows), truncated=truncated)
