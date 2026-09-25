"""The Skill product contract: what the guidance must say and must never say (RS-SKILL-01).

The Skill is the only product surface whose artifact *is* prose, so there is no runtime boundary to
test against - the boundary is what the text instructs the model to do. These checks lock the two
things that matter and nothing else:

* **Required concepts.** The current MCP tool names and the ``Test -> Create -> Scan`` order must be
  present, because a Skill that omits them will improvise a path the product does not have.
* **Forbidden legacy concepts.** The retired ``register_datasource`` bridge, the obsolete
  SQLite-only planner description, and any instruction to send a secret value through MCP must be
  absent.

Deliberately not a snapshot: wording, structure and examples may be rewritten freely as long as
these concepts hold. The tool names are imported from the real server so a rename in the MCP layer
fails here instead of silently drifting from the documentation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from qaneris.interfaces.mcp import server

SKILL_DIR = Path(server.__file__).resolve().parents[3] / "skill"
SKILL_PATH = SKILL_DIR / "SKILL.md"
EXAMPLES_PATH = SKILL_DIR / "references" / "behavior-examples.md"

#: Names a reader could mistake for a tool but which are not registered - checked so a rename in
#: the MCP layer surfaces here rather than silently drifting from the documentation.
_TOOL_LIKE = {"register_datasource"}

#: Verb prefixes MCP tool names use. An identifier starting with one is treated as a claimed tool.
_TOOL_VERBS = (
    "list_",
    "get_",
    "search_",
    "inspect_",
    "test_",
    "create_",
    "update_",
    "delete_",
    "scan_",
    "summarize_",
)

#: Backticked names in the guidance that are deliberately not tools: contract field names that share
#: a tool's vocabulary (``scan_version`` reads like ``scan_*``) but name data, not an operation.
#: The never-built credential tools are *not* listed here - they are asserted absent below, which is
#: the stronger claim.
_NOT_TOOLS = {"scan_version"}


def _paragraph_containing(text: str, *markers: str) -> str:
    """Return the smallest blank-line-separated region that mentions every marker."""
    for block in re.split(r"\n\s*\n", text):
        if all(marker in block for marker in markers):
            return block
    raise AssertionError(f"no paragraph mentions all of {markers}")


def _assert_ordered(text: str, *steps: str) -> None:
    """Assert the steps appear in this order inside one paragraph of the guidance."""
    block = _paragraph_containing(text, *steps)
    positions = [block.index(step) for step in steps]
    assert positions == sorted(positions), f"{steps} are out of order in: {block!r}"


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def examples_text() -> str:
    return EXAMPLES_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def skill_plus_examples(skill_text: str, examples_text: str) -> str:
    return f"{skill_text}\n{examples_text}"


@pytest.fixture(scope="module")
def real_tool_names() -> set[str]:
    return {tool.name for tool in server.mcp._tool_manager.list_tools()}


# -- the artifacts exist and are the ones the product ships -----------------------------------


def test_skill_artifacts_exist() -> None:
    assert SKILL_PATH.is_file()
    assert EXAMPLES_PATH.is_file()


def test_frontmatter_keeps_the_registered_skill_name(skill_text: str) -> None:
    assert skill_text.startswith("---\n")
    assert "name: qaneris-analytics" in skill_text


# -- required product concepts ----------------------------------------------------------------


def test_skill_documents_the_ask_path(skill_text: str) -> None:
    assert "ask_data" in skill_text


def test_skill_documents_the_secure_datasource_tools(skill_text: str) -> None:
    for tool in ("test_secure_datasource", "create_secure_datasource", "scan_datasource"):
        assert tool in skill_text, tool


def test_skill_documents_test_then_create_then_scan_order(skill_text: str) -> None:
    """The order is the contract; a Skill that reorders it would skip the connection test."""
    _assert_ordered(
        skill_text, "test_secure_datasource", "create_secure_datasource", "scan_datasource"
    )


def test_skill_documents_test_then_update_then_scan_order(skill_text: str) -> None:
    """An update invalidates the previous scan, so the re-scan has to be part of the order."""
    _assert_ordered(
        skill_text, "test_secure_datasource", "update_secure_datasource", "scan_datasource"
    )


def test_skill_documents_that_create_stops_at_created(skill_text: str) -> None:
    """``created`` is an intermediate state; the Skill must not describe it as answerable."""
    paragraph = _paragraph_containing(skill_text, "create_secure_datasource", "created")
    assert "ready" in paragraph
    assert "not" in paragraph.lower()


def test_skill_keeps_the_sqlite_convenience_tool(skill_text: str) -> None:
    assert "register_sqlite" in skill_text


def test_skill_frames_ask_data_as_the_only_normal_ask_path(skill_text: str) -> None:
    """No second question path: ``ask_data`` plus the source listing that feeds it."""
    assert "ask_data" in skill_text
    assert "list_datasources" in skill_text


def test_skill_treats_clarification_as_a_normal_outcome(skill_text: str) -> None:
    assert "clarification_required" in skill_text


def test_skill_reports_progress_as_execution_status(skill_text: str) -> None:
    """Progress is a stage name; the guidance must say so and rule out the reasoning reading."""
    lowered = skill_text.lower()
    assert "progress" in lowered
    assert "chain of thought" in lowered or "思维链" in skill_text
    assert "execution status" in lowered or "执行状态" in skill_text


def test_skill_routes_missing_credentials_to_a_safe_product_entry(skill_text: str) -> None:
    assert "Qaneris CLI" in skill_text
    assert "HTTP API" in skill_text
    assert "SecretReference" in skill_text


def test_skill_answers_from_the_public_response_fields(skill_text: str) -> None:
    for field in ("answer", "result", "evidence"):
        assert field in skill_text, field


def test_skill_does_not_instruct_reading_private_analysis(skill_text: str) -> None:
    """``analysis`` is an internal field; the Skill must not build answers on it."""
    assert "`analysis`" not in skill_text


# -- forbidden legacy concepts ----------------------------------------------------------------


def test_skill_does_not_reference_the_removed_bridge(skill_text: str) -> None:
    assert "register_datasource" not in skill_text


def test_examples_do_not_reference_the_removed_bridge(examples_text: str) -> None:
    assert "register_datasource" not in examples_text


def test_skill_does_not_keep_the_obsolete_sqlite_only_capability_text(
    skill_plus_examples: str,
) -> None:
    """The old planner-scope paragraph claimed a fixed SQLite-only capability envelope.

    That was a statement about one implementation stage. The current contract is that ``ask_data``
    decides, so the Skill must not pre-reject questions on the old grounds.
    """
    lowered = skill_plus_examples.lower()
    assert "current deterministic planning supports" not in lowered
    assert "deterministic planning" not in lowered
    assert "single-source sqlite" not in lowered
    assert "sqlite-only" not in lowered
    assert "arbitrary multi-table grouping" not in lowered


@pytest.mark.parametrize(
    "phrase",
    [
        "把密码给",
        "paste your password",
        "send the password",
        "pass the token",
        "upload the certificate",
        "上传私钥",
    ],
)
def test_skill_never_instructs_sending_raw_material(phrase: str, skill_plus_examples: str) -> None:
    assert phrase not in skill_plus_examples


def test_skill_explicitly_forbids_raw_secret_passthrough(skill_plus_examples: str) -> None:
    """The rule has to be stated, not merely absent: this is the one the model could violate.

    Checked as a concept (a stated refusal plus a worked counter-example), not as fixed wording, so
    the sentence may be rewritten as long as the prohibition survives.
    """
    assert "Never ask the user to paste into an MCP call" in skill_plus_examples
    assert "不要回显" in skill_plus_examples or "do not echo" in skill_plus_examples.lower()


@pytest.mark.parametrize(
    "name",
    ["create_password", "upload_certificate", "upload_private_key", "credential_add"],
)
def test_skill_does_not_offer_a_credential_creation_tool(name: str, skill_plus_examples: str) -> None:
    """MCP consumes references; it has no way to create a secret, so the Skill must not imply one."""
    assert name not in skill_plus_examples


def test_skill_does_not_present_the_unbuilt_web_entry_as_available(skill_plus_examples: str) -> None:
    """CLI and HTTP API exist today; the Web credential UI does not, and saying otherwise would
    send a user to a screen that cannot help them."""
    assert "Web" in skill_plus_examples
    assert "尚未可用" in skill_plus_examples or "not available yet" in skill_plus_examples


# -- the guidance only names tools that exist -------------------------------------------------


def test_skill_only_names_real_mcp_tools(skill_plus_examples: str, real_tool_names: set[str]) -> None:
    """Every backticked identifier shaped like a tool verb must be one that is actually registered.

    This is what catches the Skill drifting behind a rename in the MCP layer: ``scan_datasource``
    becoming ``scan_source`` would leave every other test in this file passing.
    """
    mentioned = set(re.findall(r"`([a-z][a-z_]*)(?:\([^`]*\))?`", skill_plus_examples))
    shaped_like_a_tool = {
        name
        for name in mentioned
        if name.startswith(_TOOL_VERBS) or name.endswith("_datasource") or name in _TOOL_LIKE
    }
    unknown = shaped_like_a_tool - real_tool_names - _NOT_TOOLS
    assert not unknown, f"Skill names tools that do not exist: {sorted(unknown)}"
