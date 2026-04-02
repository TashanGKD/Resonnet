"""Tools: load profile-helper skills, docs, and template assets."""

from __future__ import annotations

from pathlib import Path

from app.core.config import get_profile_helper_root

_DEFAULT_SKILL_NAMES = [
    "collect-basic-info",
    "administer-ams",
    "administer-rcss",
    "administer-mini-ipip",
    "infer-profile-dimensions",
    "review-profile",
    "update-profile",
    "generate-forum-profile",
    "generate-ai-memory-prompt",
    "import-ai-memory",
    "modify-profile-schema",
]

_DEFAULT_DOC_NAMES = [
    "academic-motivation-scale",
    "mini-ipip-scale",
    "researcher-cognitive-style",
    "tashan-profile-outline",
    "tashan-profile-examples",
    "multidimensional-work-motivation-scale",
    "implementation-guide",
]


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in deduped:
            deduped.append(resolved)
    return deduped


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _external_helper_repo() -> Path:
    return _project_root().parent / "tashan-profile-helper"


def _candidate_skill_dirs() -> list[Path]:
    primary = get_profile_helper_root() / "skills"
    builtin = Path("/app/libs_builtin/profile_helper/skills")
    local = _project_root() / "backend" / "libs" / "profile_helper" / "skills"
    external = _external_helper_repo() / "web" / "skills"
    return _dedupe_paths([external, primary, builtin, local])


def _candidate_doc_dirs() -> list[Path]:
    primary = get_profile_helper_root() / "docs"
    builtin = Path("/app/libs_builtin/profile_helper/docs")
    local = _project_root() / "backend" / "libs" / "profile_helper" / "docs"
    external = _external_helper_repo() / "doc"
    return _dedupe_paths([external, primary, builtin, local])


def _candidate_template_paths() -> list[Path]:
    primary = get_profile_helper_root() / "_template.md"
    builtin = Path("/app/libs_builtin/profile_helper/_template.md")
    local = _project_root() / "backend" / "libs" / "profile_helper" / "_template.md"
    external = _external_helper_repo() / "profiles" / "_template.md"
    return _dedupe_paths([external, primary, builtin, local])


def _resolve_existing_dir(candidates: list[Path]) -> Path:
    for path in candidates:
        if path.exists() and path.is_dir():
            return path
    return candidates[0]


def _resolve_existing_file(candidates: list[Path]) -> Path:
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return candidates[0]


def _skills_dir() -> Path:
    return _resolve_existing_dir(_candidate_skill_dirs())


def _docs_dir() -> Path:
    return _resolve_existing_dir(_candidate_doc_dirs())


def _template_path() -> Path:
    return _resolve_existing_file(_candidate_template_paths())


def list_skill_names() -> list[str]:
    skills_dir = _skills_dir()
    if skills_dir.exists() and skills_dir.is_dir():
        names = sorted(
            p.name
            for p in skills_dir.iterdir()
            if p.is_dir() and (p / "SKILL.md").exists()
        )
        if names:
            return names
    return _DEFAULT_SKILL_NAMES.copy()


def list_doc_names() -> list[str]:
    docs_dir = _docs_dir()
    if docs_dir.exists() and docs_dir.is_dir():
        names = sorted(p.stem for p in docs_dir.glob("*.md") if p.is_file())
        if names:
            return names
    return _DEFAULT_DOC_NAMES.copy()


SKILL_NAMES = list_skill_names()
DOC_NAMES = list_doc_names()


def read_skill(skill_name: str) -> str:
    """Read specified Skill file content."""
    skill_names = list_skill_names()
    if skill_name not in skill_names:
        return f"错误：未知的 skill 名称 '{skill_name}'。可用：{', '.join(skill_names)}"
    path = _skills_dir() / skill_name / "SKILL.md"
    if not path.exists():
        return f"错误：文件不存在 {path}"
    return path.read_text(encoding="utf-8")


def read_doc(doc_name: str) -> str:
    """Read reference doc from docs directory."""
    doc_names = list_doc_names()
    if doc_name not in doc_names:
        return f"错误：未知的 doc 名称 '{doc_name}'。可用：{', '.join(doc_names)}"
    path = _docs_dir() / f"{doc_name}.md"
    if not path.exists():
        return f"错误：文件不存在 {path}"
    return path.read_text(encoding="utf-8")


def load_template() -> str:
    """Load profile template."""
    template_path = _template_path()
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return "# 科研数字分身\n\n（空白模板）\n"
