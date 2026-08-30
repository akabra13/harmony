"""Structural invariants: the claims the README makes, asserted.

Every test here checks a property of the *codebase* rather than of its behaviour.
They exist because the claims they check are the ones most likely to erode quietly:
nobody notices the day a manufacturing noun appears in the kernel, and by the time
somebody does, there are forty of them.

These are cheap to run and they fail with a message that says what to do about it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KERNEL = REPO / "harmony"
COMPANY = REPO / "northfield"


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- the kernel knows nothing about manufacturing ------------------------------

DOMAIN_NOUNS = [
    "purchase_order",
    "supplier",
    "part_id",
    "production_order",
    "lot_id",
    "quality_lot",
    "goods_receipt",
    "shortage",
    "northfield",
    "erp",
    "PO-77",
    "P-4471",
]

# Matched on word boundaries. Without them "erp" matches inside "fingerprint",
# which is a false positive that would train people to ignore this test — the worst
# thing that can happen to an invariant check.
NOUN_PATTERN = re.compile(
    "|".join(rf"\b{re.escape(noun)}\b" for noun in DOMAIN_NOUNS), re.IGNORECASE
)


def test_the_kernel_contains_no_manufacturing_vocabulary():
    """``harmony/`` is a product; ``northfield/`` is a customer.

    If this fails, something domain-specific has leaked into the harness. The fix is
    almost never to add the word to the allow-list below — it is to move the concept
    into ``northfield/`` behind an existing interface, or to introduce a
    domain-neutral one.

    Comments and docstrings are exempt: the kernel's documentation refers to
    purchase orders constantly, because concrete examples are how the reasoning is
    explained. It is the *code* that must not know.
    """
    offences: list[str] = []

    for path in _python_files(KERNEL):
        code = _strip_comments_and_docstrings(_source(path))
        for match in NOUN_PATTERN.finditer(code):
            line = code[: match.start()].count("\n") + 1
            offences.append(f"{path.relative_to(REPO)}:{line} mentions '{match.group()}'")

    assert not offences, (
        "manufacturing vocabulary has leaked into the kernel:\n  "
        + "\n  ".join(offences[:20])
    )


def test_the_kernel_never_imports_the_company():
    """The only edge between the packages is the Deployment record, which the
    company hands to the harness rather than the harness reaching for."""
    offences = []
    for path in _python_files(KERNEL):
        tree = ast.parse(_source(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.startswith("northfield"):
                    offences.append(f"{path.relative_to(REPO)}:{node.lineno} imports {name}")

    assert not offences, "the kernel imports the company:\n  " + "\n  ".join(offences)


# --- time is injected ----------------------------------------------------------


def test_nothing_outside_the_clock_module_reads_the_wall_clock():
    """The demo depends on advancing time. A stray ``datetime.now()`` would make one
    code path immune to that, and the bug would appear only in the follow-up."""
    allowed = {KERNEL / "kernel" / "clock.py", KERNEL / "audit" / "log.py"}
    pattern = re.compile(r"datetime\.now\(|datetime\.utcnow\(|date\.today\(|time\.time\(")
    offences = []

    for path in _python_files(KERNEL) + _python_files(COMPANY):
        if path in allowed:
            continue
        code = _strip_comments_and_docstrings(_source(path))
        for match in pattern.finditer(code):
            line = code[: match.start()].count("\n") + 1
            offences.append(f"{path.relative_to(REPO)}:{line} — {match.group()}")

    assert not offences, (
        "wall-clock time is read outside the clock module; inject a Clock instead:\n  "
        + "\n  ".join(offences)
    )


# --- writes go through one place -----------------------------------------------


def test_system_connectors_are_reachable_only_from_providers_and_tools():
    """The tool invoker is the only path to a side effect.

    Providers read; tools write; both go through a chokepoint that checks scopes and
    audits. A detector or a demo importing a connector directly would bypass both.
    """
    allowed_prefixes = (
        "northfield/providers/",
        "northfield/tools/",
        "northfield/systems/",
        "northfield/directory.py",
        "northfield/rules.py",
        "northfield/seed/",
        "northfield/detectors/",  # read-only, and only via the broker in practice
        "northfield/demo/",  # narration reads state to display it
        "tests/",
    )
    offences = []

    for path in _python_files(COMPANY):
        relative = path.relative_to(REPO).as_posix()
        if relative.startswith(allowed_prefixes):
            continue
        tree = ast.parse(_source(path), filename=str(path))
        for node in ast.walk(tree):
            module = (
                node.module
                if isinstance(node, ast.ImportFrom)
                else (node.names[0].name if isinstance(node, ast.Import) else None)
            )
            if module and "northfield.systems" in module:
                offences.append(f"{relative}:{node.lineno} imports {module}")

    assert not offences, (
        "a system connector is reachable outside the provider/tool layer:\n  "
        + "\n  ".join(offences)
    )


def test_every_write_tool_declares_scopes():
    """A write with no scope requirement is a write anyone can perform."""
    from harmony.tools.catalog import ToolCatalog

    import northfield  # noqa: F401  (registers the catalog)
    import harmony.schedule.tools  # noqa: F401

    offenders = [
        spec.name for spec in ToolCatalog().all() if spec.writes and not spec.scopes
    ]
    assert not offenders, f"write tools with no scopes: {offenders}"


def test_every_write_tool_declares_a_rollback_position():
    """Either it can be undone and says how, or it cannot and says so.

    Silence is the one thing not allowed: a workflow ordering its steps by
    reversibility can only do that if every tool has stated where it stands.
    """
    from harmony.tools.catalog import ToolCatalog

    import northfield  # noqa: F401
    import harmony.schedule.tools  # noqa: F401

    catalog = ToolCatalog()
    # A compensation tool is itself a write and is the end of the chain; it needs no
    # compensation of its own.
    compensations = {s.compensation for s in catalog.all() if s.compensation}

    undeclared = [
        spec.name
        for spec in catalog.all()
        if spec.writes and spec.compensation is None and spec.name not in compensations
    ]
    # production.notify_supervisor is deliberately irreversible and documented as such.
    assert set(undeclared) <= {"production.notify_supervisor", "erp.record_goods_receipt"}, (
        f"write tools that have not said whether they can be undone: {undeclared}"
    )


# --- helpers -------------------------------------------------------------------


def _strip_comments_and_docstrings(source: str) -> str:
    """Blank out comments and string literals, preserving line numbers.

    The kernel's *documentation* talks about purchase orders on almost every page,
    because explaining a general mechanism without a concrete example is how you
    write documentation nobody reads. What must stay domain-free is the code.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover
        return source

    lines = source.split("\n")
    spans: list[tuple[int, int, int, int]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            spans.append(
                (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
            )

    for start_line, start_col, end_line, end_col in spans:
        for line_no in range(start_line, end_line + 1):
            index = line_no - 1
            if index >= len(lines):
                continue
            begin = start_col if line_no == start_line else 0
            finish = end_col if line_no == end_line else len(lines[index])
            lines[index] = (
                lines[index][:begin]
                + " " * max(0, finish - begin)
                + lines[index][finish:]
            )

    return "\n".join(re.sub(r"#.*$", "", line) for line in lines)


# --- profiles are internally consistent ----------------------------------------


def test_every_profile_offers_only_tools_it_could_actually_invoke():
    """A profile that advertises a tool it lacks the scope for is a configuration
    bug with an expensive symptom: the planner is shown an option, spends a model
    call reasoning about it, and the invoker then refuses. Caught at start-up
    instead, by ``Harness._validate_startup``.

    Note what this does *not* check: the tools a bound *workflow* uses. The
    production planner may enter ``po_reroute`` and cannot complete it, which is
    the situation the brief describes and the gate handles by denying the plan
    whole. That is an organisational fact, not a misconfiguration.
    """
    import tempfile

    from harmony.llm.client import StubClient
    from harmony.runtime.discovery import load_deployment
    from harmony.runtime.harness import Harness

    with tempfile.TemporaryDirectory() as tmp:
        harness = Harness.build(
            load_deployment(),
            db_path=Path(tmp) / "profiles.db",
            llm=StubClient(),
            seed=True,
        )
        for profile in harness.profiles.all():
            declared = profile.scope_set()
            assert declared is not None, f"profile '{profile.id}' declares no scopes"
            for spec in harness.tools.matching(profile.tools):
                assert spec.scopes <= declared, (
                    f"profile '{profile.id}' offers '{spec.name}' but does not declare "
                    f"{sorted(spec.scopes - declared)}"
                )
        harness.close()
