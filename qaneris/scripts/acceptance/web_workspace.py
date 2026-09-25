"""RS-WEB-01 real product acceptance entry point.

This composes the existing HTTP Excel fixture/governance acceptance and its Node browser-contract
flow. The frontend acceptance now uses the real POST SSE client, validates the terminal event chain,
and exercises a clarification stream. It intentionally keeps the established fixture and graph
bootstrap helpers in ``excel_web_ask`` as the single acceptance source of truth.
"""

import shutil

from qaneris.graph import Neo4jConfig
from qaneris.llm.config import resolve_model_profile
from qaneris.scripts.acceptance.excel_web_ask import main

if __name__ == "__main__":
    try:
        Neo4jConfig.from_environment()
    except ValueError as error:
        print(f"[BLOCKED] real Web acceptance needs Neo4j: {error}")
        raise SystemExit(2)
    if resolve_model_profile() is None:
        print("[BLOCKED] real Web acceptance needs a configured model gateway")
        raise SystemExit(2)
    if shutil.which("node") is None:
        print("[BLOCKED] real Web acceptance needs Node.js")
        raise SystemExit(2)
    raise SystemExit(main())
