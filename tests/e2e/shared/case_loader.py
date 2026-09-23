"""Load Writer functional and performance cases from their YAML registries."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Case:
    scenario: str
    case_num: int
    prompt_text: str
    attachment_path: Path | None = None
    assertions: dict | None = None
    extras: dict = field(default_factory=dict)

    @property
    def has_attachment(self) -> bool:
        return self.attachment_path is not None

    @property
    def has_feishu_reference(self) -> bool:
        """True when the case embeds a Feishu document reference."""
        return bool((self.extras or {}).get("feishu_reference"))


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

    Functional scenarios carry prompt + expected in YAML; Feishu scenarios use
    a ``feishu_doc`` key whose placeholder is resolved via ``feishu_docs``.
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
    provider_registry = data.get("provider_docs") or {}
    overlap = set(provider_registry) & set(data.get("feishu_docs") or {})
    if overlap:
        raise ValueError(f"duplicate document registry keys: {sorted(overlap)}")
    provider_key = str(scenario.get("provider_doc") or "")
    if provider_key and provider_key not in provider_registry:
        raise ValueError(f"unknown provider_doc: {provider_key}")
    text = substitute_feishu_placeholders(raw_text, rules_path=rules, repo_root=root)
    def replace_provider(match):
        key = match.group(1)
        if key not in provider_registry:
            return match.group(0)
        record = provider_registry[key] or {}
        reference = record.get("reference")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f"empty provider reference: {key}")
        return reference
    text = re.sub(r"\$\{([^}]+)\}", replace_provider, text)
    if re.search(r"\$\{[^}]+\}", text):
        raise ValueError(f"unresolved case placeholder in {scenario_id}: {text}")
    fixture_name = ((request.get("attachment") or {}).get("fixture"))
    fixture = (data.get("fixtures") or {}).get(fixture_name) if fixture_name else None

    attachment_path: Path | None = None
    extras: dict = {
        "writer_e2e": True,
        "rules_path": str(rules),
        "fixture_kind": fixture.get("kind") if fixture else None,
    }
    if provider_key:
        extras["provider_doc"] = provider_key
        extras["provider_reference"] = dict(provider_registry[provider_key])
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
            relative = Path(str(fixture["path"]))
            attachment_path = (
                relative if relative.is_absolute()
                else root.joinpath("tests", "e2e", "writer-test", relative)
            )

    expected = scenario.get("expected") or {}
    tools = expected.get("tools") or {}
    artifacts = expected.get("artifacts") or {}
    final = expected.get("final") or {}
    write_back = expected.get("write_back") or {}
    slots_required: dict = {}
    supported_artifact_rules = {
        "extension", "representation", "stage", "min_revision", "provider",
        "selected", "min_items", "success", "task_type", "patch_type", "schema",
    }
    for slot_id, rule in (artifacts.get("required") or {}).items():
        mapped = dict(rule or {})
        unknown = set(mapped) - supported_artifact_rules
        if unknown:
            raise ValueError(
                f"unsupported artifact assertion(s) for {scenario_id}.{slot_id}: "
                f"{sorted(unknown)}"
            )
        if slot_id == "draft_document" and final.get("representation"):
            mapped["representation"] = final["representation"]
        mapped.setdefault("min_revision", 1)
        slots_required[str(slot_id)] = mapped
    # ``final`` describes the selected draft_document.
    if final:
        draft_rule = dict(slots_required.get("draft_document") or {})
        for key in ("extension", "representation", "stage", "provider"):
            if final.get(key) is not None:
                draft_rule[key] = final[key]
        draft_rule["selected"] = True
        draft_rule.setdefault("min_revision", 1)
        slots_required["draft_document"] = draft_rule
    provider_revision = "any"
    if write_back.get("provider_revision_increases"):
        provider_revision = "increase"
    elif write_back.get("provider_revision_unchanged"):
        provider_revision = "unchanged"
    assertions: dict = {
        "route": str(expected.get("route") or ""),
        "steps": [str(item) for item in (expected.get("steps") or [])],
        "workspace_facts": {
            str(phase): dict(facts or {})
            for phase, facts in (expected.get("workspaces") or {}).items()
        },
        "tools_required": [str(item) for item in (tools.get("required") or [])],
        "tools_forbidden": [str(item) for item in (tools.get("forbidden") or [])],
        "slots_required": slots_required,
        "slots_forbidden": [str(item) for item in (artifacts.get("forbidden") or [])],
        "write_back": {
            "calls": int(write_back.get("calls") or 0),
            "tool": str(write_back.get("tool") or ""),
            "mode": str(write_back.get("mode") or ""),
            "provider_revision": provider_revision,
        },
    }
    for optional_key in (
            "workspace_facts", "tools_required", "tools_forbidden", "slots_forbidden"):
        if not assertions[optional_key]:
            assertions.pop(optional_key)
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
    if expected.get("provider_materialization"):
        assertions["provider_materialization"] = bool(
            expected["provider_materialization"])
    if expected.get("sse"):
        assertions["sse"] = dict(expected["sse"])
    if expected.get("content"):
        assertions["content"] = dict(expected["content"])
    if final.get("provider"):
        assertions["provider"] = str(final["provider"])
    if expected.get("modify_types"):
        assertions["modify_types"] = [str(item) for item in expected["modify_types"]]
    if "image_calls" in expected:
        assertions["image_calls"] = int(expected["image_calls"])
    if "ui_editable" in final:
        assertions["ui_editable"] = bool(final["ui_editable"])
    if "new_revision" in final:
        assertions["new_revision"] = bool(final["new_revision"])

    return Case(
        scenario=scenario_id.upper(),
        case_num=0,
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
    if re.search(r"\$\{[^}]+\}", text):
        raise ValueError(
            f"unresolved case placeholder in {scenario_id}.{case_num}: {text}"
        )

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
        prompt_text=text,
        attachment_path=attachment_path,
        extras=extras,
    )
