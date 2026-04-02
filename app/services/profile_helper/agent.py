"""LLM tool calling + agentic loop for profile helper."""

import json
import logging
import os
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

MAX_TOOL_ITERATIONS = max(
    5, int(os.getenv("PROFILE_HELPER_MAX_TOOL_ITERATIONS", "40"))
)
READ_ONLY_STREAK_LIMIT = max(
    6, int(os.getenv("PROFILE_HELPER_READ_ONLY_STREAK_LIMIT", "18"))
)
READ_ONLY_TOOLS = {"read_skill", "read_doc", "read_profile"}
logger = logging.getLogger(__name__)


def _summarize_tool_arguments(args: dict, max_len: int = 240) -> str:
    """Return compact, truncated JSON for safe tool-call logging."""
    try:
        raw = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        raw = str(args)
    raw = raw.replace("\n", "\\n")
    if len(raw) <= max_len:
        return raw
    return f"{raw[: max_len - 3]}..."


def _normalize_tool_call(name: str, args: dict) -> tuple[str, dict]:
    """
    Normalize model-emitted tool names.
    Some models may emit skill/doc names directly (e.g. infer-profile-dimensions)
    instead of calling read_skill/read_doc with arguments.
    """
    known_tools = {
        "read_skill",
        "read_doc",
        "read_profile",
        "write_profile",
        "write_forum_profile",
    }
    if name in known_tools:
        return name, args

    if name in list_skill_names():
        return "read_skill", {"skill_name": name}

    if name in list_doc_names():
        return "read_doc", {"doc_name": name}

    return name, args


def _next_read_only_streak(current_streak: int, tool_names: list[str]) -> int:
    """Advance or reset streak based on whether this iteration is read-only."""
    if tool_names and all(name in READ_ONLY_TOOLS for name in tool_names):
        return current_streak + 1
    return 0


def _fallback_finalize_content() -> str:
    return (
        "我检测到当前请求在重复读取资料，暂时先停止工具循环。"
        "我可以基于目前信息给出阶段性结论，或按你的优先级继续推进某一维度。"
    )


def _finalize_without_tools(
    client,
    model: str,
    system_content: str,
    messages: list[dict],
) -> str:
    """Force a direct answer without tools when loop is stuck."""
    forced_system = (
        f"{system_content}\n\n"
        "【系统约束】你刚才出现了重复工具调用。"
        "现在禁止调用任何工具，请基于已有信息直接给出阶段性结论，"
        "并明确列出还缺失的关键信息（最多3条）。"
    )
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": forced_system}] + messages,
    )
    msg = response.choices[0].message
    content = (msg.content or "").strip()
    return content or _fallback_finalize_content()


def _build_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_skill",
                "description": "读取指定 Skill 文件，获取具体任务的操作指南。执行任务前必须先调用此工具。",
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
                "description": "读取参考文档（量表原题等）。施测时用此工具获取题目。",
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
                "description": "获取当前会话中的科研数字分身内容。每次开始任务前先调用，了解当前填写进度和采集阶段。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_profile",
                "description": "将科研数字分身内容写入会话并同步保存到 profiles 目录。创建和更新过程中每获得一轮可保存信息后都应立即调用此工具。",
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
                "description": "将他山论坛分身写入会话并同步保存到 profiles 目录。当用户确认生成后，用此工具保存内容。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "完整的他山论坛分身 Markdown（Identity/Expertise/Thinking Style/Discussion Style 四节格式）",
                        }
                    },
                    "required": ["content"],
                },
            },
        },
    ]


def _execute_tool(name: str, args: dict, session: dict) -> str:
    """Execute a single tool and return its result."""
    if name == "read_skill":
        return read_skill(args.get("skill_name", ""))
    if name == "read_doc":
        return read_doc(args.get("doc_name", ""))
    if name == "read_profile":
        return session["profile"]
    if name == "write_profile":
        content = args.get("content", "")
        path = save_profile(session, content)
        return f"已写入科研数字分身并保存到 {path.name}，共 {len(content)} 字符。"
    if name == "write_forum_profile":
        content = args.get("content", "")
        path = save_forum_profile(session, content)
        return f"已写入他山论坛分身并保存到 {path.name}，共 {len(content)} 字符。"
    return f"未知工具: {name}"


def run_agent(
    user_message: str,
    session: dict,
    *,
    stream: bool = False,
    model: str | None = None,
):
    """
    Run agent loop. session is from sessions.get().
    If stream=True, yield each chunk; else yield full reply.
    model: optional override for this request; defaults to AI_GENERATION_MODEL.
    """
    client = create_client()
    if not client:
        yield "错误：未配置 AI 生成 API。请在 .env 中设置 AI_GENERATION_BASE_URL 和 AI_GENERATION_API_KEY。"
        return

    model = model or get_default_model()
    today_str = date.today().strftime("%Y-%m-%d")
    system_content = (
        META_SYSTEM_PROMPT
        + f"\n\n**当前日期**：{today_str}（写入画像时，创建时间、最后更新、unnamed 文件名等请使用此日期）"
    )
    messages = session["messages"].copy()
    messages.append({"role": "user", "content": user_message})

    max_iterations = MAX_TOOL_ITERATIONS
    tool_call_counts: dict[str, int] = {}
    read_only_streak = 0

    for iteration in range(max_iterations):
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_content}] + messages,
            tools=_build_tools(),
            tool_choice="auto",
        )

        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if tool_calls:
            call_summaries: list[dict[str, str]] = []
            for tc in tool_calls:
                try:
                    parsed_args = (
                        json.loads(tc.function.arguments) if tc.function.arguments else {}
                    )
                except json.JSONDecodeError:
                    parsed_args = {"_raw": tc.function.arguments or ""}
                normalized_name, normalized_args = _normalize_tool_call(
                    tc.function.name,
                    parsed_args,
                )
                if normalized_name != tc.function.name:
                    logger.warning(
                        "profile_helper tool_alias session_id=%s original=%s normalized=%s",
                        session.get("session_id"),
                        tc.function.name,
                        normalized_name,
                    )
                tool_call_counts[normalized_name] = (
                    tool_call_counts.get(normalized_name, 0) + 1
                )
                call_summaries.append(
                    {
                        "name": normalized_name,
                        "args": _summarize_tool_arguments(normalized_args),
                    }
                )

            logger.info(
                "profile_helper tool_calls session_id=%s iteration=%s/%s calls=%s details=%s",
                session.get("session_id"),
                iteration + 1,
                max_iterations,
                len(tool_calls),
                json.dumps(call_summaries, ensure_ascii=False),
            )

            iteration_tool_names = [item["name"] for item in call_summaries]
            read_only_streak = _next_read_only_streak(
                read_only_streak,
                iteration_tool_names,
            )
            if read_only_streak >= READ_ONLY_STREAK_LIMIT:
                logger.warning(
                    "profile_helper read_only_loop_break session_id=%s streak=%s iteration=%s/%s tool_counts=%s",
                    session.get("session_id"),
                    read_only_streak,
                    iteration + 1,
                    max_iterations,
                    json.dumps(tool_call_counts, ensure_ascii=False, sort_keys=True),
                )
                content = _finalize_without_tools(client, model, system_content, messages)
                session["messages"] = messages + [{"role": "assistant", "content": content}]
                if stream:
                    for i in range(0, len(content), 1):
                        yield content[i : i + 1]
                else:
                    yield content
                return

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                normalized_name, normalized_args = _normalize_tool_call(
                    tc.function.name,
                    args,
                )
                result = _execute_tool(normalized_name, normalized_args, session)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )
            continue

        content = (msg.content or "").strip()
        logger.info(
            "profile_helper completed session_id=%s iteration=%s total_tool_calls=%s tool_counts=%s",
            session.get("session_id"),
            iteration + 1,
            sum(tool_call_counts.values()),
            json.dumps(tool_call_counts, ensure_ascii=False, sort_keys=True),
        )
        session["messages"] = messages + [{"role": "assistant", "content": content}]

        if stream:
            for i in range(0, len(content), 1):
                yield content[i : i + 1]
        else:
            yield content
        return

    err = "达到最大工具调用次数，请简化请求后重试。"
    logger.warning(
        "profile_helper max_tool_iterations_reached session_id=%s max_iterations=%s total_tool_calls=%s tool_counts=%s",
        session.get("session_id"),
        max_iterations,
        sum(tool_call_counts.values()),
        json.dumps(tool_call_counts, ensure_ascii=False, sort_keys=True),
    )
    session["messages"] = messages + [{"role": "assistant", "content": err}]
    if stream:
        for c in err:
            yield c
    else:
        yield err
