from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from qaneris.graph.neo4j import Neo4jGraphReader, Neo4jGraphStore
from qaneris.graph.ports import GraphReader, GraphStore, NullGraphReader, NullGraphStore


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    username: str
    password: str
    database: str = "neo4j"

    def __repr__(self) -> str:
        return (
            "Neo4jConfig("
            f"uri={self.uri!r}, username={self.username!r}, "
            "password=<redacted>, "
            f"database={self.database!r})"
        )

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> Neo4jConfig:
        env = environment if environment is not None else os.environ
        names = {
            "uri": "QANERIS_NEO4J_URI",
            "username": "QANERIS_NEO4J_USERNAME",
            "password": "QANERIS_NEO4J_PASSWORD",
        }
        values = {field: env.get(name, "").strip() for field, name in names.items()}
        missing = [name for field, name in names.items() if not values[field]]
        if missing:
            raise ValueError(f"Missing required Neo4j configuration: {', '.join(missing)}")
        return cls(
            **values,
            database=env.get("QANERIS_NEO4J_DATABASE", "neo4j").strip() or "neo4j",
        )


def graph_store_from_environment(
    environment: Mapping[str, str] | None = None,
) -> GraphStore:
    env = environment if environment is not None else os.environ
    backend = env.get("QANERIS_GRAPH_STORE", "neo4j").strip().lower()
    if backend == "null":
        return NullGraphStore()
    if backend != "neo4j":
        raise ValueError(f"Unsupported QANERIS_GRAPH_STORE: {backend}")
    config = Neo4jConfig.from_environment(env)
    return Neo4jGraphStore(
        config.uri,
        config.username,
        config.password,
        database=config.database,
    )


def graph_reader_from_environment(
    environment: Mapping[str, str] | None = None,
) -> GraphReader:
    """Read side factory sharing the same ``QANERIS_GRAPH_STORE`` switch as the write side."""
    env = environment if environment is not None else os.environ
    backend = env.get("QANERIS_GRAPH_STORE", "neo4j").strip().lower()
    if backend == "null":
        return NullGraphReader()
    if backend != "neo4j":
        raise ValueError(f"Unsupported QANERIS_GRAPH_STORE: {backend}")
    config = Neo4jConfig.from_environment(env)
    return Neo4jGraphReader(
        config.uri,
        config.username,
        config.password,
        database=config.database,
    )
