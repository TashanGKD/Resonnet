"""Extract structured profile fields from profile markdown."""

from __future__ import annotations

import re


def _extract_section(md: str, heading: str) -> str:
    pattern = rf"##\s+{re.escape(heading)}\s*\n(.*?)(?=\n##\s|\Z)"
    match = re.search(pattern, md, re.DOTALL)
    return match.group(1).strip() if match else ""


def _extract_field(text: str, label: str) -> str:
    pattern = rf"\*\*{re.escape(label)}\*\*[：:]\s*(.*?)(?:\n|$)"
    match = re.search(pattern, text)
    if not match:
        return ""
    value = match.group(1).strip()
    if value.startswith("<!--") or not value:
        return ""
    return value


def _extract_table_rows(text: str) -> list[dict]:
    rows = []
    lines = text.split("\n")
    headers: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        if not headers:
            headers = cells
            continue
        row = {}
        for index, header in enumerate(headers):
            row[header] = cells[index] if index < len(cells) else ""
        rows.append(row)
    return rows


def _extract_number(text: str) -> float | None:
    match = re.search(r"[-+]?\d+\.?\d*", text)
    return float(match.group()) if match else None


def _is_filled(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and not value.strip().startswith("<!--")
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, dict):
        return any(_is_filled(v) for v in value.values())
    if isinstance(value, list):
        return len(value) > 0
    return bool(value)


def parse_profile(md: str) -> dict:
    result: dict = {
        "name": "",
        "meta": {"created_at": "", "updated_at": "", "stage": "", "source": ""},
        "identity": {},
        "capability": {"tech_stack": [], "process": {}, "outputs": ""},
        "needs": {"time_occupation": [], "pain_points": [], "want_to_change": ""},
        "cognitive_style": {},
        "motivation": {
            "dimensions": {},
            "intrinsic_total": None,
            "extrinsic_total": None,
            "rai": None,
            "source": "",
        },
        "personality": {},
        "interpretation": {"core_driver": "", "risks": "", "path": ""},
        "completion": {},
    }

    title_match = re.search(r"^#\s+科研人员画像\s*[—–-]\s*(.+)", md, re.MULTILINE)
    if title_match:
        name = title_match.group(1).strip()
        if name and name not in ("[姓名/标识]", "姓名/标识"):
            result["name"] = name

    meta = _extract_section(md, "元信息")
    if meta:
        result["meta"]["created_at"] = _extract_field(meta, "创建时间")
        result["meta"]["updated_at"] = _extract_field(meta, "最后更新")
        result["meta"]["stage"] = _extract_field(meta, "采集阶段")
        result["meta"]["source"] = _extract_field(meta, "数据来源")

    identity_section = _extract_section(md, "一、基础身份")
    if identity_section:
        result["identity"] = {
            "research_stage": _extract_field(identity_section, "研究阶段"),
            "primary_field": _extract_field(identity_section, "一级领域"),
            "secondary_field": _extract_field(identity_section, "二级领域"),
            "cross_field": _extract_field(identity_section, "交叉方向"),
            "method": _extract_field(identity_section, "方法范式"),
            "institution": _extract_field(identity_section, "所在机构"),
            "network": _extract_field(identity_section, "学术网络"),
        }

    capability_section = _extract_section(md, "二、能力")
    if capability_section:
        tech_match = re.search(
            r"###\s+2\.1\s+技术能力\s*\n(.*?)(?=###|\Z)",
            capability_section,
            re.DOTALL,
        )
        if tech_match:
            rows = _extract_table_rows(tech_match.group(1))
            tech_stack = []
            for row in rows:
                category = row.get("类别", "")
                tech = row.get("具体技术", "")
                level = row.get("熟练程度（★☆）", row.get("熟练程度", ""))
                if tech:
                    tech_stack.append({"category": category, "tech": tech, "level": level})
            result["capability"]["tech_stack"] = tech_stack

        outputs_match = re.search(
            r"\*\*代表性产出\*\*[：:]\s*(.*?)(?=###|\Z)", capability_section, re.DOTALL
        )
        if outputs_match:
            value = outputs_match.group(1).strip()
            if not value.startswith("<!--"):
                result["capability"]["outputs"] = value

        process_match = re.search(
            r"###\s+2\.2\s+科研流程能力\s*\n(.*?)(?=---|\Z)",
            capability_section,
            re.DOTALL,
        )
        if process_match:
            process_rows = _extract_table_rows(process_match.group(1))
            mapping = {
                "问题定义": "problem_definition",
                "文献整合": "literature",
                "方案设计": "design",
                "实验执行": "execution",
                "论文写作": "writing",
                "项目管理": "management",
            }
            for row in process_rows:
                label = row.get("环节", "").strip()
                score_str = row.get("评分", "").strip()
                description = row.get("简要说明", "").strip()
                key = mapping.get(label)
                if key and score_str:
                    number = _extract_number(score_str)
                    result["capability"]["process"][key] = {
                        "score": number,
                        "description": (
                            description if description and not description.startswith("<!--") else ""
                        ),
                    }

    needs_section = _extract_section(md, "三、当前需求")
    if needs_section:
        occupation_match = re.search(
            r"###\s+3\.1\s+主要时间占用\s*\n(.*?)(?=###|\Z)",
            needs_section,
            re.DOTALL,
        )
        if occupation_match:
            rows = _extract_table_rows(occupation_match.group(1))
            result["needs"]["time_occupation"] = [
                {
                    "item": row.get("事项", ""),
                    "desc": row.get("描述", ""),
                    "feeling": row.get("感受", ""),
                }
                for row in rows
                if row.get("事项")
            ]

        pain_match = re.search(
            r"###\s+3\.2\s+核心难点与卡点\s*\n(.*?)(?=###|\Z)",
            needs_section,
            re.DOTALL,
        )
        if pain_match:
            rows = _extract_table_rows(pain_match.group(1))
            result["needs"]["pain_points"] = [
                {
                    "issue": row.get("难点", ""),
                    "detail": row.get("具体表现", ""),
                    "help_type": row.get("期望获得的帮助类型", ""),
                }
                for row in rows
                if row.get("难点")
            ]

        change_match = re.search(
            r"###\s+3\.3\s+近期最想改变的一件事\s*\n(.*?)(?=---|\Z)",
            needs_section,
            re.DOTALL,
        )
        if change_match:
            value = change_match.group(1).strip()
            if not value.startswith("<!--"):
                result["needs"]["want_to_change"] = value

    cognitive_section = _extract_section(md, "四、认知风格（RCSS）")
    if cognitive_section:
        source_match = re.search(r"数据来源[：:]\s*`?([^`\n]+)`?", cognitive_section)
        source = source_match.group(1).strip() if source_match else ""
        summary_rows = _extract_table_rows(cognitive_section)
        cognitive_data: dict = {"source": source}
        for row in summary_rows:
            indicator = row.get("指标", "")
            score_str = row.get("得分", row.get("", ""))
            number = _extract_number(score_str) if score_str else None
            if "横向整合分" in indicator and number is not None:
                cognitive_data["integration"] = number
            elif "垂直深度分" in indicator and number is not None:
                cognitive_data["depth"] = number
            elif "认知风格指数" in indicator and number is not None:
                cognitive_data["csi"] = number
            elif "认知风格类型" in indicator:
                cognitive_data["type"] = score_str.strip() if score_str else ""
        result["cognitive_style"] = cognitive_data

    motivation_section = _extract_section(md, "五、学术动机（AMS-GSR 28）")
    if motivation_section:
        source_match = re.search(r"数据来源[：:]\s*`?([^`\n]+)`?", motivation_section)
        result["motivation"]["source"] = source_match.group(1).strip() if source_match else ""

        dim_mapping = {
            "求知内在动机": "know",
            "成就内在动机": "accomplishment",
            "体验刺激内在动机": "stimulation",
            "认同调节": "identified",
            "内摄调节": "introjected",
            "外部调节": "external",
            "无动机": "amotivation",
        }
        dim_rows = _extract_table_rows(motivation_section)
        for row in dim_rows:
            label = row.get("维度", row.get("指标", "")).strip()
            score_str = row.get("平均分（1–7）", row.get("数值", row.get("得分", ""))).strip()
            number = _extract_number(score_str) if score_str else None
            key = dim_mapping.get(label)
            if key and number is not None:
                result["motivation"]["dimensions"][key] = number
            elif "内在动机总分" in label and number is not None:
                result["motivation"]["intrinsic_total"] = number
            elif "外在动机总分" in label and number is not None:
                result["motivation"]["extrinsic_total"] = number
            elif "自主动机指数" in label and number is not None:
                result["motivation"]["rai"] = number

    personality_section = _extract_section(md, "六、人格（Mini-IPIP）")
    if personality_section:
        source_match = re.search(r"数据来源[：:]\s*`?([^`\n]+)`?", personality_section)
        source = source_match.group(1).strip() if source_match else ""
        mapping = {
            "外向性": "extraversion",
            "宜人性": "agreeableness",
            "尽责性": "conscientiousness",
            "神经质": "neuroticism",
            "开放性/智力": "openness",
        }
        rows = _extract_table_rows(personality_section)
        personality_data: dict = {"source": source}
        for row in rows:
            label = row.get("维度", "").strip()
            for zh, en in mapping.items():
                if zh in label:
                    score_str = row.get("平均分（1–5）", "").strip()
                    number = _extract_number(score_str) if score_str else None
                    level = row.get("水平描述", "").strip()
                    if number is not None:
                        personality_data[en] = {"score": number, "level": level}
                    break
        result["personality"] = personality_data

    interpretation_section = _extract_section(md, "七、综合解读")
    if interpretation_section:
        driver_match = re.search(
            r"###\s+核心驱动模式\s*\n(.*?)(?=###|\Z)",
            interpretation_section,
            re.DOTALL,
        )
        risks_match = re.search(
            r"###\s+潜在风险与发展建议\s*\n(.*?)(?=###|\Z)",
            interpretation_section,
            re.DOTALL,
        )
        path_match = re.search(
            r"###\s+适合的发展路径\s*\n(.*?)(?=---|\Z)",
            interpretation_section,
            re.DOTALL,
        )
        result["interpretation"]["core_driver"] = (
            driver_match.group(1).strip() if driver_match else ""
        )
        result["interpretation"]["risks"] = risks_match.group(1).strip() if risks_match else ""
        result["interpretation"]["path"] = path_match.group(1).strip() if path_match else ""

    result["completion"] = {
        "identity": _is_filled(result["identity"]),
        "capability": _is_filled(result["capability"]["process"]),
        "needs": _is_filled(result["needs"]["time_occupation"])
        or _is_filled(result["needs"]["want_to_change"]),
        "cognitive_style": _is_filled(result["cognitive_style"].get("csi")),
        "motivation": _is_filled(result["motivation"]["dimensions"]),
        "personality": any(
            k != "source" and _is_filled(v) for k, v in result["personality"].items()
        ),
        "interpretation": _is_filled(result["interpretation"]["core_driver"]),
    }
    return result
