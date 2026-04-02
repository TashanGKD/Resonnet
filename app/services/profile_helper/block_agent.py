"""Block 协议 Agent：返回 Block 列表供前端结构化渲染。

与 streaming agent 并存，通过 /profile-helper/chat/blocks 端点调用。
Block 类型：text / choice / text_input / rating / chart / actions / copyable

快速路径（fast path）绕过 LLM，直接返回静态模板块，节省 token 并消除等待：
- WELCOME 路径：首次消息（空会话）→ 欢迎说明 + A/B 选择题（不调用 LLM）
- AI_MEMORY 路径：用户选择 A（AI记忆导入）→ 直接返回完整提示词 copyable block
"""

import json
import os
import re
from datetime import date

from app.services.profile_helper.llm_client import create_client, get_default_model
from app.services.profile_helper.prompts import META_SYSTEM_PROMPT
from app.services.profile_helper.sessions import save_forum_profile, save_profile
from app.services.profile_helper.tools import (
    list_doc_names,
    list_skill_names,
    read_doc,
    read_skill,
)

# ── 后端工具（执行后结果喂回 LLM）─────────────────────────────────

def _build_backend_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_skill",
                "description": "读取指定 Skill 文件，获取具体任务的操作指南。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "enum": list_skill_names(),
                            "description": "Skill 名称",
                        }
                    },
                    "required": ["skill_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_doc",
                "description": "读取参考文档（量表原题等）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "doc_name": {
                            "type": "string",
                            "enum": list_doc_names(),
                            "description": "文档名称",
                        }
                    },
                    "required": ["doc_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_profile",
                "description": "获取当前会话中的科研数字分身内容。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_profile",
                "description": "将科研数字分身内容写入会话并保存。每获得一轮信息后都应立即调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "完整的科研数字分身 Markdown 内容",
                        }
                    },
                    "required": ["content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_forum_profile",
                "description": "将他山论坛分身写入会话并保存。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "完整的他山论坛分身 Markdown",
                        }
                    },
                    "required": ["content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_profile_completeness",
                "description": (
                    "扫描当前画像，检查 F1-F7 必填字段是否都已填写。"
                    "在 import-ai-memory 整合完成后、决定下一步之前调用。"
                    "返回各字段填写状态和缺失列表。"
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]

# ── UI 工具（转换为 Block 发送给前端）────────────────────────────

_UI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ask_choice",
            "description": "向用户展示一个单选题。前端渲染为可点击的按钮组。每次只问一个问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "问题文本"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                                "text_prompt": {
                                    "type": "string",
                                    "description": "若设置，点击该选项后前端在按钮右侧展示内联输入框。值为输入框的 placeholder 文字。适用于「需要修改」「其他」类选项，让用户无需二次点击即可填写补充内容。",
                                },
                            },
                            "required": ["id", "label"],
                        },
                        "description": "选项列表",
                    },
                },
                "required": ["question", "options"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_text",
            "description": "向用户提一个开放式问题。前端渲染为输入框。每次只问一个问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "问题文本"},
                    "placeholder": {"type": "string", "description": "输入框提示文字"},
                    "multiline": {"type": "boolean", "description": "是否多行输入"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_rating",
            "description": "请用户对某项进行评分。前端渲染为评分按钮行。每次只问一个问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "评分问题"},
                    "min_val": {"type": "integer", "description": "最小分值"},
                    "max_val": {"type": "integer", "description": "最大分值"},
                    "min_label": {"type": "string", "description": "最低分标签"},
                    "max_label": {"type": "string", "description": "最高分标签"},
                },
                "required": ["question", "min_val", "max_val"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_profile_chart",
            "description": "展示画像可视化图表（雷达图或柱状图）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chart_type": {
                        "type": "string",
                        "enum": ["radar", "bar"],
                        "description": "图表类型",
                    },
                    "title": {"type": "string", "description": "图表标题"},
                    "dimensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "维度名称列表",
                    },
                    "values": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "各维度数值",
                    },
                    "max_value": {"type": "number", "description": "最大值（归一化用）"},
                },
                "required": ["chart_type", "title", "dimensions", "values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_copyable",
            "description": "展示一段需要用户复制的固定文本（如提示词模板）。前端渲染为带「一键复制」按钮的内容框。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "标题（可选）"},
                    "content": {"type": "string", "description": "需要用户复制的完整文本内容"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_actions",
            "description": "展示操作按钮，如跳转到画像页或量表测试。在完成关键步骤后使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "提示文字"},
                    "buttons": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "label": {"type": "string"},
                                "href": {"type": "string"},
                                "style": {
                                    "type": "string",
                                    "enum": ["primary", "secondary"],
                                },
                            },
                            "required": ["id", "label"],
                        },
                    },
                },
                "required": ["buttons"],
            },
        },
    },
]

_UI_TOOL_NAMES = {t["function"]["name"] for t in _UI_TOOLS}
_INTERACTIVE_UI_TOOLS = {"ask_choice", "ask_text", "ask_rating"}
_DISPLAY_UI_TOOLS = {"show_copyable", "show_profile_chart", "show_actions"}


def _execute_backend_tool(name: str, args: dict, session: dict) -> str:
    if name == "read_skill":
        skill = args.get("skill_name", "")
        if skill in list_skill_names():
            return read_skill(skill)
        return f"未知 Skill: {skill}"
    if name == "read_doc":
        doc = args.get("doc_name", "")
        if doc in list_doc_names():
            return read_doc(doc)
        return f"未知文档: {doc}"
    if name == "read_profile":
        return session["profile"]
    if name == "write_profile":
        content = args.get("content", "")
        path = save_profile(session, content)
        return f"已写入科研数字分身并保存：{path.name}，共 {len(content)} 字符。"
    if name == "write_forum_profile":
        content = args.get("content", "")
        path = save_forum_profile(session, content)
        return f"已写入他山论坛分身并保存：{path.name}，共 {len(content)} 字符。"
    if name == "check_profile_completeness":
        return json.dumps(_check_profile_completeness(session.get("profile", "")), ensure_ascii=False)
    return f"未知工具: {name}"


def _check_profile_completeness(profile: str) -> dict:
    """解析画像 Markdown，检查 F1-F7 必填字段是否有实质内容。"""
    placeholders = {"待填写", "—", "-", "", "TBD", "暂无", "[待填写]"}

    def has_content(pattern: str) -> bool:
        m = re.search(pattern, profile, re.MULTILINE)
        if not m:
            return False
        value = m.group(1).strip()
        return value not in placeholders and len(value) > 0

    f1 = has_content(r"研究阶段[：:]\s*(.+)")
    f2 = has_content(r"一级领域[：:]\s*(.+)")
    f3 = has_content(r"方法范式[：:]\s*(.+)")

    # F4：科研流程能力表格中至少 3 个维度有数字评分
    flow_rows = re.findall(
        r"\|\s*(?:问题定义|文献整合|方案设计|实验执行|论文写作|项目管理)[^|]*\|\s*([1-5])\s*\|",
        profile,
    )
    f4 = len(flow_rows) >= 3

    # F5：技术能力表格至少 1 行（有类别和具体技术）
    tech_rows = re.findall(r"\|\s*\S.+?\|\s*\S.+?\|\s*[★☆✦]{1,5}", profile)
    f5 = len(tech_rows) >= 1

    # F6：主要时间占用（3.1）有实质内容
    f6_match = re.search(r"### 3\.1.*?\n(.*?)(?=###|\Z)", profile, re.DOTALL)
    f6 = bool(f6_match and len(f6_match.group(1).strip()) > 10)

    # F7：核心难点（3.2）有实质内容
    f7_match = re.search(r"### 3\.2.*?\n(.*?)(?=###|\Z)", profile, re.DOTALL)
    f7 = bool(f7_match and len(f7_match.group(1).strip()) > 10)

    missing = []
    if not f1:
        missing.append("F1：研究阶段")
    if not f2:
        missing.append("F2：一级领域")
    if not f3:
        missing.append("F3：方法范式")
    if not f4:
        missing.append("F4：科研流程能力（至少3维度评分）")
    if not f5:
        missing.append("F5：技术能力（至少1条）")
    if not f6:
        missing.append("F6：主要时间占用（3.1，至少1条）")
    if not f7:
        missing.append("F7：核心难点（3.2，至少1条）")

    return {
        "all_required_filled": len(missing) == 0,
        "missing_fields": missing,
        "detail": {
            "F1_research_stage": f1,
            "F2_primary_field": f2,
            "F3_method": f3,
            "F4_process_scores": f4,
            "F5_tech_stack": f5,
            "F6_time_occupation": f6,
            "F7_pain_points": f7,
        },
    }


def _ui_tool_to_block(name: str, args: dict) -> dict:
    """将 UI 工具调用转换为前端 Block。"""
    if name == "ask_choice":
        return {
            "type": "choice",
            "id": args.get("question", "")[:20],
            "question": args.get("question", ""),
            # options 已包含 text_prompt 字段（若 LLM 设置了的话），直接透传
            "options": args.get("options", []),
        }
    if name == "ask_text":
        return {
            "type": "text_input",
            "id": args.get("question", "")[:20],
            "question": args.get("question", ""),
            "placeholder": args.get("placeholder", ""),
            "multiline": args.get("multiline", False),
        }
    if name == "ask_rating":
        return {
            "type": "rating",
            "id": args.get("question", "")[:20],
            "question": args.get("question", ""),
            "min_val": args.get("min_val", 1),
            "max_val": args.get("max_val", 5),
            "min_label": args.get("min_label", ""),
            "max_label": args.get("max_label", ""),
        }
    if name == "show_profile_chart":
        return {
            "type": "chart",
            "chart_type": args.get("chart_type", "radar"),
            "title": args.get("title", ""),
            "dimensions": args.get("dimensions", []),
            "values": args.get("values", []),
            "max_value": args.get("max_value", 5),
        }
    if name == "show_copyable":
        return {
            "type": "copyable",
            "title": args.get("title", ""),
            "content": args.get("content", ""),
        }
    if name == "show_actions":
        return {
            "type": "actions",
            "message": args.get("message", ""),
            "buttons": args.get("buttons", []),
        }
    return {"type": "text", "content": f"[未知UI工具: {name}]"}


def _format_tool_call(tc) -> dict:
    return {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.function.name,
            "arguments": tc.function.arguments or "{}",
        },
    }


# ── 快速路径：静态模板，不调用 LLM ─────────────────────────────

_PRIVACY_NOTICE = """\
你好！欢迎使用**他山数字分身系统**。

在开始建立你的科研数字分身之前，先告知一些重要的**隐私与安全信息**：

- 您在本系统中提供的所有信息仅用于构建和更新您的数字分身。平台不会向任何第三方泄露您的数据，也不会将您的数据用于模型训练或其他用途。
- 您的数字分身仅在平台内部运行，用于与系统中的其他智能体进行信息交流与协作，不会在平台之外使用。
- 您可以自行决定该数字分身是否公开。当选择公开时，其他用户在发起讨论或协作任务时可以选择您的数字分身参与；当选择不公开时，该数字分身仅对您本人可见和使用。

---

在开始逐项填写之前，想问一下——你平时有没有使用过**带记忆功能的 AI 工具**？
（比如 ChatGPT、Claude、Gemini 等，且已经有一定时间的使用记录）

如果有的话，我可以帮你生成一段提示词，发给你使用的 AI，让它根据对你的了解来预填科研数字分身——这样能节省不少时间。
若你使用多个 AI，可把同一提示词都发一遍，再把回复依次粘贴回来，我会帮你整合。\
"""

_WELCOME_BLOCKS: list[dict] = [
    {"type": "text", "content": _PRIVACY_NOTICE},
    {
        "type": "choice",
        "id": "start_method",
        "question": "你希望如何开始建立科研数字分身？",
        "options": [
            {
                "id": "ai_memory",
                "label": "A. 有，我想先从 AI 记忆中提取信息",
                "description": "让 AI 根据聊天记忆预填信息，节省时间",
            },
            {
                "id": "direct",
                "label": "B. 没有，或者不需要，直接开始填写",
                "description": "通过对话逐步填写各维度信息",
            },
        ],
    },
]

# AI 记忆提示词：全量模板（5 个模块，适用于新用户）
_AI_MEMORY_PROMPT_TEMPLATE = """\
【科研数字分身信息提取请求】

你好！我正在使用一个科研数字分身系统（他山数字分身系统）来记录和分析我的科研状态。
请根据你对我的了解，**依次回答**以下问题。

⚠️ 重要说明：
1. 请仅根据我们真实对话中已出现的信息作答，严禁推测或捏造
2. 如果某项你没有足够的记忆依据，请直接写「记忆不足，无法确认」
3. 所有信息将由我本人核对后才会写入画像，你的回答只作为参考
4. 请尽量保留我在对话中的原话（verbatim），便于核对来源

---

【模块 A：基础身份】
请根据你对我的了解，**依次回答**以下问题（每项用1-2句话，不确定则写"记忆不足"）：

A1. 我目前处于哪个研究阶段？（博士生 / 博士后 / 青年教师 / PI / 其他）
A2. 我的主要研究领域是什么？（一级学科 + 具体方向）
A3. 我主要采用哪种研究方法？（实验法 / 理论推导 / 计算建模 / 数据驱动 / 质性 / 混合）
A4. 我所在的机构是哪里？导师或团队研究方向是什么？
A5. 我的学术合作圈大概是什么情况？

---

【模块 B：科研能力】
请根据你对我的了解，**依次回答**以下问题：

B1. 我主要使用哪些编程语言或科研工具？熟练程度如何？
B2. 我是否有代表性的学术产出（论文、开源项目、工具包等）？如有请简述。
B3. 在以下 6 个科研流程环节中，你观察到我哪些比较强、哪些相对薄弱？
    - 问题定义
    - 文献整合
    - 研究方案设计
    - 实验/计算执行
    - 论文写作
    - 项目与时间管理
    （请以「较强」/「一般」/「较弱」/「记忆不足」作答）

---

【模块 C：当前需求】
请根据你对我最近对话的了解，**依次回答**以下问题：

C1. 我最近花费最多精力的事情是什么？（包括科研以外的事务也可以提及）
C2. 我最近提到过哪些困扰、卡点或让我觉得"推不动"的事情？
C3. 我最近是否表达过"最想改变" / "最想突破"某件事？如有，是什么？

⚠️ 当前需求是高度个人化的信息，请格外审慎，仅基于我明确表达过的内容作答。

---

【模块 D：认知风格参照】
以下是关于科研认知风格的两种类型描述，请根据你对我的了解，说明你观察到的倾向：

- 横向整合型：喜欢跨领域连接，善于整合不同方法和理论，享受"拼图"思维
- 垂直深度型：喜欢深挖一个问题，追求单一领域的极致精通，享受"打井"式钻研

D1. 在我们的对话中，我更像哪种类型？
D2. 有没有具体的例子或话语能支持你的判断？

⚠️ 这不是量表测量，只作为参考，最终数据以我自填量表为准。

---

【模块 E：动机与人格参照】
以下是几个关于科研动机和人格的维度，请根据你对我的了解，给出定性描述：

E1. 我从事科研的主要动力来源是什么？（知识好奇心？成就感？外部压力？职业发展？）
E2. 我是否曾表达过对科研意义的质疑或困惑？
E3. 在人格层面，我给你的整体印象是？（例如：开放好奇 / 谨慎负责 / 活跃外向 / 情绪稳定 等）

⚠️ 这不是量表测量，只作为参考，最终数据以我自填量表为准。

---
【汇总格式要求】
请将你的回答按以上模块**依次作答**，每项均注明可信度标签，并附上依据：

- ✅ 有据可查：必须附上具体证据，例如「你在[日期/话题]中提到过……」或摘录你的原话
- ⚠️ 印象模糊：说明模糊程度及不确定来源
- ❌ 记忆不足：直接写明「记忆不足，无法确认」即可

谢谢！\
"""

_AI_MEMORY_USAGE_NOTE = """\
以上是为你生成的提示词，适用于 ChatGPT、Claude、Gemini 等所有带记忆功能的 AI。

**使用建议：**
1. 直接粘贴到对话框发送即可，无需修改
2. 如果你使用多个 AI 工具，请将同一提示词分别发送给每个 AI，然后把每个 AI 的回复**依次粘贴**回来，我会帮你整合
3. 如果 AI 回复"记忆不足"较多，可以追问：「你记得我跟你聊过关于[具体话题]的事情吗？」
4. 拿到 AI 的回复后，回来把内容粘贴给我，我会帮你整合进科研数字分身

⚠️ 安全提示：这份提示词不会让 AI 泄露你的具体对话内容，只是请它根据已有记忆做定向总结。\
"""

_AI_MEMORY_BLOCKS: list[dict] = [
    {"type": "text", "content": "好的！以下是为你生成的 AI 记忆提取提示词："},
    {
        "type": "copyable",
        "title": "📋 AI 记忆提取提示词（点击一键复制）",
        "content": _AI_MEMORY_PROMPT_TEMPLATE,
    },
    {"type": "text", "content": _AI_MEMORY_USAGE_NOTE},
]

# 触发欢迎流程的关键词
_WELCOME_TRIGGERS = frozenset([
    "帮我建立分身", "新建分身", "开始", "建立分身", "建立科研数字分身",
    "新建档案", "开始收集信息", "hello", "你好", "hi", "start",
    "帮我建立", "创建分身", "帮我创建",
    "建立我的分身",  # 前端默认初始消息
])

# 触发 AI 记忆路径的关键词
_AI_MEMORY_TRIGGERS = frozenset([
    "a.", "a、", "a：", "a:",
    "ai记忆", "ai 记忆", "从ai", "从 ai",
    "有，我想先从 ai 记忆中提取信息",
    "有，我想先从ai记忆中提取信息",
    "a. 有，我想先从 ai 记忆中提取信息",
    "a. 有，我想先从ai记忆中提取信息",
    "generate-ai-memory-prompt",
    "从chatgpt", "从 chatgpt", "从claude", "从 claude",
    "chatgpt记忆", "claude记忆",
])


def _is_fresh_session_or_init_msg(session: dict, user_message: str) -> bool:
    """判断是否应触发欢迎流程（空会话 OR 典型开始指令）"""
    history = session.get("messages", [])
    # 空会话：没有任何消息
    if not history:
        return True
    # 只有一条用户消息，且是典型开始指令（可能是重置后的第一条）
    if len(history) == 1:
        msg_lower = user_message.strip().lower()
        if msg_lower in _WELCOME_TRIGGERS:
            return True
    return False


def _is_ai_memory_request(user_message: str, session: dict) -> bool:
    """判断用户是否在请求 AI 记忆导入路径"""
    msg_lower = user_message.strip().lower()
    # 精确匹配
    if msg_lower in _AI_MEMORY_TRIGGERS:
        return True
    # 前缀匹配（如 "a." 开头）
    if re.match(r"^a[.、：:]\s*", msg_lower):
        return True
    # 关键词包含匹配
    for kw in ("ai记忆", "ai 记忆", "从ai", "从 ai", "chatgpt记忆", "claude记忆"):
        if kw in msg_lower:
            return True
    return False


def run_block_agent(
    user_message: str,
    session: dict,
    *,
    model: str | None = None,
) -> list[dict]:
    """
    运行 Block 协议 Agent 循环。返回 Block 列表供前端渲染。
    Block 类型：text / choice / text_input / rating / chart / actions / copyable
    """
    # ── 快速路径 1：首次消息 → 欢迎模板（不调用 LLM）──────────────
    if _is_fresh_session_or_init_msg(session, user_message):
        # 将用户消息和欢迎回复加入会话历史，保持上下文完整
        session["messages"].append({"role": "user", "content": user_message})
        session["messages"].append({"role": "assistant", "content": _PRIVACY_NOTICE})
        return _WELCOME_BLOCKS

    # ── 快速路径 2：AI 记忆路径 → 直接返回静态提示词（不调用 LLM）──
    if _is_ai_memory_request(user_message, session):
        session["messages"].append({"role": "user", "content": user_message})
        session["messages"].append({
            "role": "assistant",
            "content": "已生成 AI 记忆提取提示词，请复制后发送给你使用的 AI。",
        })
        return _AI_MEMORY_BLOCKS

    # ── 正常 LLM 路径 ──────────────────────────────────────────────
    client = create_client()
    if not client:
        return [{"type": "text", "content": "错误：未配置 AI 生成 API。"}]

    model = model or get_default_model()
    today_str = date.today().strftime("%Y-%m-%d")
    system_content = (
        META_SYSTEM_PROMPT
        + f"\n\n**当前日期**：{today_str}（写入画像时使用此日期。）"
    )

    messages = session["messages"].copy()
    messages.append({"role": "user", "content": user_message})

    all_tools = _build_backend_tools() + _UI_TOOLS
    response_blocks: list[dict] = []
    max_iterations = max(10, int(os.getenv("PROFILE_HELPER_BLOCK_MAX_ITERATIONS", "30")))

    for _ in range(max_iterations):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system_content}] + messages,
                tools=all_tools,
                tool_choice="auto",
            )
        except Exception as e:
            response_blocks.append({"type": "text", "content": f"LLM 调用失败: {e}"})
            break

        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            if msg.content and msg.content.strip():
                response_blocks.append({"type": "text", "content": msg.content.strip()})
            break

        if msg.content and msg.content.strip():
            response_blocks.append({"type": "text", "content": msg.content.strip()})

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [_format_tool_call(tc) for tc in tool_calls],
            }
        )

        has_interactive_ui = False
        interactive_block_count = 0  # 单次响应内的交互 Block 计数
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}

            if tc.function.name in _INTERACTIVE_UI_TOOLS:
                # 每轮只允许一个交互型 Block，多余的拦截并告知 LLM 下轮再问
                if interactive_block_count >= 1:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": (
                            "【系统限制】每轮对话只能向用户展示一个问题。"
                            "此问题已被忽略，请等用户回答当前问题后，在下一轮中再提问。"
                        ),
                    })
                    continue
                block = _ui_tool_to_block(tc.function.name, args)
                response_blocks.append(block)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "已展示给用户，请等待用户回复后再继续。不要在本轮继续提问。",
                })
                has_interactive_ui = True
                interactive_block_count += 1
            elif tc.function.name in _DISPLAY_UI_TOOLS:
                block = _ui_tool_to_block(tc.function.name, args)
                response_blocks.append(block)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "已展示给用户。",
                })
            else:
                # 后端工具：执行后喂回 LLM
                result = _execute_backend_tool(tc.function.name, args, session)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        if has_interactive_ui:
            break

    # 更新会话消息历史
    session["messages"] = messages

    # 将最后的文本 Block 追加到 assistant 角色（保持消息历史完整）
    last_text = ""
    for b in response_blocks:
        if b["type"] == "text":
            last_text += b["content"]

    if last_text:
        session["messages"].append({"role": "assistant", "content": last_text})
    elif session["messages"] and session["messages"][-1].get("role") == "tool":
        # ── 关键修复：合法消息序列保障 ──────────────────────────────────
        # OpenAI 格式要求：tool_result 之后必须有 assistant 消息，
        # 不能直接跟 user 消息，否则 LLM 丢失上下文，触发欢迎流程重置。
        # 当 LLM 仅调用交互型 UI 工具（ask_text/ask_choice/ask_rating）
        # 且没有任何文字输出时，messages 末尾是 tool_result。
        # 在此插入一条 assistant 桥接消息，包含刚才展示的问题，
        # 让 LLM 在用户回复时能准确理解上下文。
        interactive_block = next(
            (b for b in response_blocks if b.get("type") in {"text_input", "choice", "rating"}),
            None,
        )
        if interactive_block:
            q = interactive_block.get("question", "")
            block_type_label = {
                "text_input": "等待用户输入",
                "choice": "等待用户选择",
                "rating": "等待用户评分",
            }.get(interactive_block["type"], "等待用户回复")
            bridging = f"（{block_type_label}：{q}）" if q else f"（{block_type_label}）"
        else:
            bridging = "（已向用户展示交互界面，等待回复）"
        session["messages"].append({"role": "assistant", "content": bridging})

    return response_blocks
