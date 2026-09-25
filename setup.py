#!/usr/bin/env python3
"""Prepare this machine to run SmartData.

Run it from the repository root:

```bash
python3 setup.py            # check, repair what is safe to repair, then report
python3 setup.py --check    # report only; change nothing
python3 setup.py --json     # machine-readable result for scripts and CI
```

What it verifies, in order: the Python and Node runtimes, Docker and Compose, ``uv``, the
``.env`` keys the API refuses to start without, the managed credential store, the Python and
frontend dependency trees, the model endpoint, the Neo4j container behind
``SMARTDATA_NEO4J_URI``, a real bolt handshake, and whether the two dev servers are already up.

Repairs stay inside the repository. ``uv`` and Node are installed under ``.tools/`` when they
are missing, dependencies go into ``.venv`` and ``web/frontend/node_modules``, and Neo4j is
started from this repository's ``docker-compose.yml``. Anything needing root, a global install
or a secret is printed as an exact command for you to run instead of being done silently.

The implementation lives in ``tools/environment_setup.py``; this file is only the entry point.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.environment_setup import main

if __name__ == "__main__":
    raise SystemExit(main())
