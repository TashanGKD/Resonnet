"""科学家匹配：预置库向量距离 + LLM 领域推荐。

移植自 digital-twin-bootstrap/backend/scientist_match.py，
适配 TopicLab 模块路径。
"""
import json
import math

from app.services.profile_helper.scientists_db import SCIENTISTS


def _normalize(val: float, lo: float, hi: float) -> float:
    rng = hi - lo
    if rng == 0:
        return 0.5
    return (val - lo) / rng


def _personality_distance(user_p: dict, sci: dict) -> float:
    """大五人格欧氏距离（归一化到 0-1）"""
    dims = ["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"]
    user_vals = []
    sci_vals = []
    for d in dims:
        u = user_p.get(d, {})
        user_vals.append((u.get("score", 3.0) if isinstance(u, dict) else 3.0) / 5.0)
        sci_vals.append(sci.get(d, 3.0) / 5.0)
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(user_vals, sci_vals)) / len(dims))


def match_famous_scientists(parsed: dict) -> dict:
    """
    返回 {
        top3: [{name, name_en, field, era, similarity, reason, signature, csi, rai}],
        scatter_data: [{name, name_en, csi, rai, is_top3}],
        user_point: {csi, rai}
    }
    """
    user_csi = parsed.get("cognitive_style", {}).get("csi")
    user_rai = parsed.get("motivation", {}).get("rai")

    if user_csi is None:
        user_csi = 0
    if user_rai is None:
        user_rai = 25

    csi_range = 48
    rai_lo, rai_hi = -20, 60
    rai_range = rai_hi - rai_lo

    W_CSI = 0.4
    W_RAI = 0.4
    W_PER = 0.2

    scored = []
    for sci in SCIENTISTS:
        csi_dist = ((user_csi - sci["csi"]) / csi_range) ** 2
        rai_dist = ((user_rai - sci["rai"]) / rai_range) ** 2
        per_dist = _personality_distance(parsed.get("personality", {}), sci) ** 2
        distance = math.sqrt(W_CSI * csi_dist + W_RAI * rai_dist + W_PER * per_dist)
        similarity = max(0, round((1 - distance) * 100))
        scored.append({**sci, "_dist": distance, "similarity": similarity})

    scored.sort(key=lambda x: x["_dist"])
    top3_names = {s["name"] for s in scored[:3]}

    top3 = []
    for s in scored[:3]:
        top3.append({
            "name": s["name"],
            "name_en": s["name_en"],
            "field": s["field"],
            "era": s["era"],
            "similarity": s["similarity"],
            "reason": s["match_reason_template"],
            "signature": s["signature"],
            "csi": s["csi"],
            "rai": s["rai"],
        })

    scatter_data = []
    for s in SCIENTISTS:
        scatter_data.append({
            "name": s["name"],
            "name_en": s["name_en"],
            "csi": s["csi"],
            "rai": s["rai"],
            "is_top3": s["name"] in top3_names,
        })

    return {
        "top3": top3,
        "scatter_data": scatter_data,
        "user_point": {"csi": user_csi, "rai": user_rai},
    }


def recommend_field_scientists(parsed: dict) -> list:
    """调用 LLM 推荐与用户领域相关的活跃科学家。"""
    from app.services.profile_helper.llm_client import create_client, get_default_model

    identity = parsed.get("identity", {})
    field_info = " / ".join(filter(None, [
        identity.get("primary_field", ""),
        identity.get("secondary_field", ""),
        identity.get("cross_field", ""),
    ]))
    method = identity.get("method", "")

    if not field_info:
        return []

    try:
        client = create_client()
        if not client:
            return []
        model = get_default_model()
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一个学术推荐助手。请根据用户的研究领域，推荐 3-5 位与其方向高度相关的"
                        "当代活跃科学家（在世或近十年活跃）。"
                        "输出 JSON 数组，每项包含 name(中文名)、name_en(英文名)、institution(机构)、"
                        "field(研究方向)、reason(推荐理由一句话)。只输出 JSON，不要其他文字。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"用户研究领域：{field_info}\n研究方法：{method}",
                },
            ],
            temperature=0.7,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        return json.loads(text)
    except Exception:
        return []
