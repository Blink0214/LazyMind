"""Loader for the ``cases/<scenario>/`` directory layout used by both modes.

A case folder may contain:

  prompt_N.md           required; the user message sent to chat
  attachment_N.<ext>    optional; the single attachment uploaded with the prompt
  document_N.md         optional; the Feishu baseline applied before the run
  case_N.yaml           optional; sidecar with functional assertions

Loading returns a ``Case`` value object.  Both perf and func runners consume
the same ``Case`` so the input contract stays consistent across modes.

Scenarios and case numbers are integers stored in directory and file names.
The loader only treats them as opaque identifiers; product code must not be
sensitive to specific values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


PROMPT_RE = re.compile(r"prompt_(\d+)\.md$")
ATTACH_RE = re.compile(r"attachment_(\d+)\.[^.]+$")
DOCUMENT_RE = re.compile(r"document_(\d+)\.md$")
SIDE_CAR_RE = re.compile(r"case_(\d+)\.ya?ml$")


@dataclass
class Case:
    scenario: str
    case_num: int
    prompt_path: Path
    prompt_text: str
    attachment_path: Path | None = None
    document_path: Path | None = None
    sidecar_path: Path | None = None
    assertions: dict | None = None
    extras: dict = field(default_factory=dict)

    @property
    def has_attachment(self) -> bool:
        return self.attachment_path is not None

    @property
    def has_functional_assertions(self) -> bool:
        return self.assertions is not None

    @property
    def has_feishu_reference(self) -> bool:
        """True when the case embeds a Feishu document reference."""
        return bool((self.extras or {}).get("feishu_reference"))


def load_case(cases_root: Path | str, scenario: str, case_num: int,
              *, load_assertions: bool = True) -> Case:
    sdir = Path(cases_root) / scenario
    if not sdir.is_dir():
        raise FileNotFoundError(f"scenario directory not found: {sdir}")

    prompt_path = sdir / f"prompt_{case_num}.md"
    if not prompt_path.is_file():
        raise FileNotFoundError(f"prompt file missing: {prompt_path}")
    prompt_text = prompt_path.read_text(encoding="utf-8").strip()

    # Prompts may embed Feishu placeholders (${<registry key>}); resolve the
    # URL and record the baseline so the runner can reset.
    extras: dict = {}
    feishu_key = next(
        (key for key in load_feishu_registry()
         if "${%s}" % key in prompt_text),
        None,
    )
    if feishu_key:
        prompt_text = substitute_feishu_placeholders(prompt_text)
        record = feishu_record_for_key(feishu_key) or {}
        extras = {
            "feishu_doc": feishu_key,
            "feishu_reference": str(record.get("url") or ""),
            "feishu_history_version_id": str(record.get("history_version_id") or ""),
            "feishu_baseline_revision_id": int(record.get("baseline_revision_id") or -1),
        }

    attachment_path = _first_match(sdir, ATTACH_RE, case_num)
    document_path = _first_match(sdir, DOCUMENT_RE, case_num)
    sidecar_path = _first_match(sdir, SIDE_CAR_RE, case_num)

    assertions = None
    if load_assertions and sidecar_path is not None:
        try:
            import yaml  # local-only; do not require PyYAML to exist at module load.
        except ImportError:
            yaml = None
        if yaml is not None:
            assertions = yaml.safe_load(sidecar_path.read_text(encoding="utf-8")) or None
        else:
            assertions = {"_raw": sidecar_path.read_text(encoding="utf-8"), "_parse": "yaml-missing"}

    return Case(
        scenario=scenario,
        case_num=case_num,
        prompt_path=prompt_path,
        prompt_text=prompt_text,
        attachment_path=attachment_path,
        document_path=document_path,
        sidecar_path=sidecar_path,
        assertions=assertions,
        extras=extras,
    )


def _first_match(sdir: Path, pattern: re.Pattern, case_num: int) -> Path | None:
    for entry in sdir.iterdir():
        m = pattern.match(entry.name)
        if m and int(m.group(1)) == case_num:
            return entry
    return None


def _expand_env(match: re.Match) -> str:
    name = match.group(1)
    value = __import__("os").environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing environment variable: {name}")
    return value


def _required_env(name: str) -> str:
    value = __import__("os").environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing environment variable: {name}")
    return value


DEFAULT_FUNC_CASES_YAML = ("tests", "e2e", "writer-test", "cases", "writer_func_cases.yaml")
DEFAULT_PERF_CASES_YAML = ("tests", "e2e", "writer-test", "cases", "writer_perf_cases.yaml")
DEFAULT_EXEC_CONSTRAINTS_YAML = ("tests", "e2e", "writer-test", "cases", "exec_constraints.yaml")


def _load_rules(rules_path: Path) -> dict:
    import yaml  # local-only; do not require PyYAML at module load.
    return yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}


def _rules_path(rules_path: Path | str | None,
                repo_root: Path) -> Path:
    return Path(rules_path or repo_root.joinpath(*DEFAULT_FUNC_CASES_YAML))


def load_exec_constraints(repo_root: Path | str | None = None) -> dict[str, str]:
    """统一执行约束提示词（write / revise 等模板），供 case loader 测试开始时注入。"""
    root = Path(repo_root or Path(__file__).resolve().parents[3])
    path = root.joinpath(*DEFAULT_EXEC_CONSTRAINTS_YAML)
    if not path.is_file():
        return {}
    data = _load_rules(path)
    return {
        str(key): str(value).strip()
        for key, value in (data or {}).items()
        if key not in ("schema_version", "suite", "description") and value
    }


def inject_exec_constraints(text: str, constraints_key: str | None,
                            repo_root: Path | str | None = None) -> str:
    """在测试开始时把统一执行约束注入到提示词末尾。"""
    key = str(constraints_key or "").strip()
    if not key:
        return text
    template = load_exec_constraints(repo_root).get(key)
    if not template:
        raise KeyError(
            f"exec constraint template not found: {key!r} "
            f"(check {DEFAULT_EXEC_CONSTRAINTS_YAML})"
        )
    return f"{text.rstrip()}\n\n{template}".strip()


def load_feishu_registry(rules_path: Path | str | None = None,
                         repo_root: Path | str | None = None) -> dict[str, dict]:
    """Feishu doc registry from the merged cases YAML (url + baseline)."""
    root = Path(repo_root or Path(__file__).resolve().parents[3])
    merged: dict[str, dict] = {}
    candidates = (
        [Path(rules_path)]
        if rules_path
        else [root.joinpath(*DEFAULT_FUNC_CASES_YAML), root.joinpath(*DEFAULT_PERF_CASES_YAML)]
    )
    for rules in candidates:
        if not rules.is_file():
            continue
        data = _load_rules(rules)
        for key, record in (data.get("feishu_docs") or {}).items():
            merged[str(key)] = dict(record or {})
    return merged


def feishu_record_for_key(key: str, *,
                          rules_path: Path | str | None = None,
                          repo_root: Path | str | None = None) -> dict | None:
    return load_feishu_registry(rules_path, repo_root).get(key)


def feishu_record_for_url(url: str, *,
                          rules_path: Path | str | None = None,
                          repo_root: Path | str | None = None) -> dict | None:
    for record in load_feishu_registry(rules_path, repo_root).values():
        if str(record.get("url") or "") == url:
            return record
    return None


def substitute_feishu_placeholders(text: str, *,
                                   rules_path: Path | str | None = None,
                                   repo_root: Path | str | None = None) -> str:
    """Replace ``${<registry key>}`` tokens (e.g. ${P3_FEISHU_OUTLINE_1}) with URLs."""
    registry = load_feishu_registry(rules_path, repo_root)
    for key, record in registry.items():
        text = text.replace("${%s}" % key, str(record.get("url") or ""))
    return text


def load_writer_e2e_scenario(scenario_id: str, *,
                             rules_path: Path | str | None = None,
                             repo_root: Path | str | None = None) -> Case:
    """Load one scenario from the func cases YAML (writer_func_cases.yaml).

    Functional scenarios (C01..C07 / I01..I02) carry prompt + expected in the
    YAML; Feishu scenarios use a ``feishu_doc`` key (with the legacy ``${...}``
    env placeholder in the prompt) resolved via the ``feishu_docs`` registry.
    Performance scenarios live in ``writer_perf_cases.yaml`` (load_perf_case).
    """
    root = Path(repo_root or Path(__file__).resolve().parents[3])
    rules = _rules_path(rules_path, root)
    if not rules.is_file():
        raise FileNotFoundError(f"cases YAML not found: {rules}")
    data = _load_rules(rules)
    scenario = next(
        (item for item in data.get("scenarios") or []
         if str(item.get("id") or "").upper() == scenario_id.upper()),
        None,
    )
    if not scenario:
        raise FileNotFoundError(f"unknown scenario in cases YAML: {scenario_id}")

    request = scenario.get("request") or {}
    raw_text = str(request.get("text") or "")
    feishu_key = str(scenario.get("feishu_doc") or "")
    if not feishu_key:
        feishu_key = next(
            (key for key in load_feishu_registry(rules_path=rules, repo_root=root)
             if "${%s}" % key in raw_text),
            "",
        )
    text = substitute_feishu_placeholders(raw_text, rules_path=rules, repo_root=root)
    text = re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", _expand_env, text)
    text = inject_exec_constraints(text, scenario.get("constraints"), repo_root=root)
    fixture_name = ((request.get("attachment") or {}).get("fixture"))
    fixture = (data.get("fixtures") or {}).get(fixture_name) if fixture_name else None

    attachment_path: Path | None = None
    extras: dict = {
        "writer_e2e": True,
        "rules_path": str(rules),
        "fixture_kind": fixture.get("kind") if fixture else None,
    }
    if feishu_key:
        record = feishu_record_for_key(feishu_key, rules_path=rules, repo_root=root) or {}
        extras["fixture_kind"] = "feishu_url"
        extras["feishu_doc"] = feishu_key
        extras["feishu_reference"] = str(record.get("url") or "")
        extras["feishu_history_version_id"] = str(record.get("history_version_id") or "")
        extras["feishu_baseline_revision_id"] = int(
            record.get("baseline_revision_id") or -1
        )
    elif fixture:
        kind = fixture.get("kind")
        if kind == "local_file":
            if fixture.get("path"):
                relative = Path(str(fixture["path"]))
                attachment_path = (
                    relative if relative.is_absolute()
                    else root.joinpath("tests", "e2e", "writer-test", relative)
                )
            else:
                configured = _required_env(str(fixture["env"]))
                attachment_path = Path(configured)
                if not attachment_path.is_absolute():
                    attachment_path = root / attachment_path
        elif kind == "feishu_url":
            extras["feishu_reference"] = _required_env(str(fixture["env"]))
            extras["feishu_history_version_id"] = _required_env(
                str(fixture["baseline_history_env"])
            )
            extras["feishu_baseline_revision_id"] = int(
                _required_env(str(fixture["baseline_revision_env"]))
            )

    expected = scenario.get("expected") or {}
    tools = expected.get("tools") or {}
    artifacts = expected.get("artifacts") or {}
    final = expected.get("final") or {}
    write_back = expected.get("write_back") or {}
    slots_required: dict = {}
    for slot_id, rule in (artifacts.get("required") or {}).items():
        mapped = dict(rule or {})
        if slot_id == "draft_document" and final.get("representation"):
            mapped["representation"] = final["representation"]
        mapped.setdefault("min_revision", 1)
        slots_required[str(slot_id)] = mapped
    provider_revision = "any"
    if write_back.get("provider_revision_increases"):
        provider_revision = "increase"
    elif write_back.get("provider_revision_unchanged"):
        provider_revision = "unchanged"
    assertions: dict = {
        "route": str(expected.get("route") or ""),
        "steps": [str(item) for item in (expected.get("steps") or [])],
        "tools_required": [str(item) for item in (tools.get("required") or [])],
        "tools_forbidden": [str(item) for item in (tools.get("forbidden") or [])],
        "slots_required": slots_required,
        "slots_forbidden": [str(item) for item in (artifacts.get("forbidden") or [])],
        "write_back": {
            "calls": int(write_back.get("calls") or 0),
            "tool": str(write_back.get("tool") or ""),
            "provider_revision": provider_revision,
        },
    }
    if expected.get("media"):
        assertions["media"] = expected["media"]
    if expected.get("cross_ref"):
        assertions["cross_ref"] = expected["cross_ref"]
    if expected.get("ref_integrity"):
        assertions["ref_integrity"] = expected["ref_integrity"]
    if expected.get("numbering"):
        assertions["numbering"] = expected["numbering"]
    if expected.get("ref_number_consistency"):
        assertions["ref_number_consistency"] = expected["ref_number_consistency"]
    if final.get("provider"):
        assertions["provider"] = str(final["provider"])
    if expected.get("modify_types"):
        assertions["modify_types"] = [str(item) for item in expected["modify_types"]]
    if "ui_editable" in final:
        assertions["ui_editable"] = bool(final["ui_editable"])
    if "new_revision" in final:
        assertions["new_revision"] = bool(final["new_revision"])

    return Case(
        scenario=scenario_id.upper(),
        case_num=0,
        prompt_path=None,
        prompt_text=text,
        attachment_path=attachment_path,
        assertions=assertions,
        extras=extras,
    )


def load_perf_case(scenario_id: str, case_num: int, *,
                   rules_path: Path | str | None = None,
                   repo_root: Path | str | None = None) -> Case:
    """Load one performance case from ``writer_perf_cases.yaml``.

    Perf prompts embed ``${AI_WRITER_FEISHU_OUTLINE_URL}`` /
    ``${AI_WRITER_FEISHU_FULL_URL}`` tokens; the case's ``feishu_doc`` key
    resolves the actual URL and baseline from the registry, so each of the
    5 cases uses its own registered document.
    """
    root = Path(repo_root or Path(__file__).resolve().parents[3])
    rules = Path(rules_path or root.joinpath(*DEFAULT_PERF_CASES_YAML))
    if not rules.is_file():
        raise FileNotFoundError(f"perf cases YAML not found: {rules}")
    data = _load_rules(rules)
    scenario = next(
        (item for item in data.get("scenarios") or []
         if str(item.get("id") or "").upper() == scenario_id.upper()),
        None,
    )
    if not scenario:
        raise FileNotFoundError(f"unknown perf scenario: {scenario_id}")
    case_data = next(
        (item for item in scenario.get("cases") or []
         if int(item.get("id") or -1) == int(case_num)),
        None,
    )
    if not case_data:
        raise FileNotFoundError(f"case {case_num} not found in {scenario_id}")

    text = str(case_data.get("text") or "")
    feishu_doc = str(case_data.get("feishu_doc") or "")
    record = (
        feishu_record_for_key(feishu_doc, rules_path=rules, repo_root=root)
        if feishu_doc else None
    )
    if record:
        text = substitute_feishu_placeholders(text, rules_path=rules, repo_root=root)
    text = re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", _expand_env, text)
    text = inject_exec_constraints(text, case_data.get("constraints"), repo_root=root)

    attachment_path: Path | None = None
    if case_data.get("attachment"):
        relative = Path(str(case_data["attachment"]))
        attachment_path = (
            relative if relative.is_absolute()
            else root.joinpath("tests", "e2e", "writer-test", relative)
        )

    extras: dict = {"perf_case": True, "rules_path": str(rules)}
    constraints_key = str(case_data.get("constraints") or "").strip()
    if constraints_key:
        extras["constraints"] = constraints_key
    if record:
        extras.update({
            "feishu_doc": feishu_doc,
            "feishu_reference": str(record.get("url") or ""),
            "feishu_history_version_id": str(record.get("history_version_id") or ""),
            "feishu_baseline_revision_id": int(record.get("baseline_revision_id") or -1),
        })
    return Case(
        scenario=scenario_id.upper(),
        case_num=int(case_num),
        prompt_path=None,
        prompt_text=text,
        attachment_path=attachment_path,
        extras=extras,
    )
