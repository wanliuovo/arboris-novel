# AIMETA P=小说API_项目和章节管理|R=小说CRUD_章节管理|NR=不含内容生成|E=route:GET_POST_/api/novels/*|X=http|A=小说CRUD_章节|D=fastapi,sqlalchemy|S=db|RD=./README.ai
import json
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.dependencies import get_current_user
from ...db.session import get_session
from ...schemas.novel import (
    Blueprint,
    BlueprintGenerationResponse,
    BlueprintPatch,
    Chapter as ChapterSchema,
    ConverseRequest,
    ConverseResponse,
    NovelQARequest,
    NovelQAResponse,
    NovelProject as NovelProjectSchema,
    NovelProjectSummary,
    NovelSectionResponse,
    NovelSectionType,
)
from ...schemas.user import UserInDB
from ...services.import_service import ImportService
from ...services.llm_service import LLMService
from ...services.novel_service import NovelService
from ...services.prompt_service import PromptService
from ...utils.json_utils import remove_think_tags, sanitize_json_like_text, unwrap_markdown_json

logger = logging.getLogger(__name__)
BLUEPRINT_GENERATION_MAX_ATTEMPTS = 5
BLUEPRINT_OUTLINE_BATCH_SIZE = 10
DEFAULT_BLUEPRINT_CHAPTERS = 30

router = APIRouter(prefix="/api/novels", tags=["Novels"])

JSON_RESPONSE_INSTRUCTION = """
IMPORTANT: 你的回复必须是合法的 JSON 对象，并严格包含以下字段：
{
  "ai_message": "string",
  "ui_control": {
    "type": "single_choice | text_input | info_display",
    "options": [
      {"id": "option_1", "label": "string"}
    ],
    "placeholder": "string"
  },
  "conversation_state": {},
  "is_complete": false
}
不要输出额外的文本或解释。
"""


def _ensure_prompt(prompt: str | None, name: str) -> str:
    if not prompt:
        raise HTTPException(status_code=500, detail=f"未配置名为 {name} 的提示词，请联系管理员")
    return prompt


def _extract_target_chapter_count_from_text(text: str) -> Optional[int]:
    if not text:
        return None

    cleaned = text.strip()
    range_match = re.search(r"(\d{1,3})\s*[-~—到至]\s*(\d{1,3})\s*章?", cleaned)
    if range_match:
        return min(int(range_match.group(2)), 200)

    plus_match = re.search(r"(\d{1,3})\s*章?\s*(?:以上|\+)", cleaned)
    if plus_match:
        return min(int(plus_match.group(1)), 200)

    exact_patterns = [
        r"(?:共|总共|一共|大概|大约|预计|计划|写|生成|需要|要)?\s*(\d{1,3})\s*章",
        r"^(\d{1,3})$",
    ]
    for pattern in exact_patterns:
        match = re.search(pattern, cleaned)
        if match:
            return min(int(match.group(1)), 200)

    if "短篇" in cleaned:
        return 20
    if "中篇" in cleaned:
        return 40
    if "长篇" in cleaned:
        return 60
    if "史诗" in cleaned:
        return 60

    return None


def _extract_target_chapter_count(history: List[Dict[str, str]]) -> Optional[int]:
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        count = _extract_target_chapter_count_from_text(item.get("content", ""))
        if count:
            return count
    return None


def _build_blueprint_constraints(target_chapters: Optional[int]) -> str:
    return f"""
[硬性蓝图主体生成约束]
用户最终选择/输入的篇幅按 {target_chapters or DEFAULT_BLUEPRINT_CHAPTERS} 章处理。
本次只生成小说蓝图主体字段，不要在这一步生成章节大纲。
chapter_outline 必须返回空数组 []，完整章节大纲会由服务端随后分批生成并校验。
必须只返回合法 JSON 对象，不要输出 Markdown 或解释文字。
"""


def _build_blueprint_core_prompt(target_chapters: int) -> str:
    return f"""
你是一位小说蓝图架构师。请根据用户的灵感对话，整理可执行的小说蓝图主体。

必须严格返回合法 JSON 对象，不要 Markdown，不要解释文字。JSON 字段必须包含：
{{
  "title": "string",
  "target_audience": "string",
  "genre": "string",
  "style": "string",
  "tone": "string",
  "one_sentence_summary": "string",
  "full_synopsis": "string",
  "world_setting": {{}},
  "characters": [],
  "relationships": [],
  "chapter_outline": []
}}

硬性要求：
1. 用户最终篇幅按 {target_chapters} 章处理，但本次不要生成章节大纲。
2. chapter_outline 必须是空数组 []，后续由服务端分批生成完整大纲。
3. characters 使用对象数组；relationships 每项必须包含 character_from、character_to、description。
4. full_synopsis 要覆盖开端、发展、高潮和结局，为后续分批大纲提供足够依据。
"""


def _normalize_chapter_outline(blueprint_data: Dict[str, Any]) -> None:
    outlines = blueprint_data.get("chapter_outline")
    if not isinstance(outlines, list):
        blueprint_data["chapter_outline"] = []
        return

    normalized = []
    for index, outline in enumerate(outlines, start=1):
        if not isinstance(outline, dict):
            continue
        item = dict(outline)
        item["chapter_number"] = int(item.get("chapter_number") or index)
        item["title"] = str(item.get("title") or f"第{item['chapter_number']}章")
        item["summary"] = str(item.get("summary") or "")
        normalized.append(item)
    normalized.sort(key=lambda item: item["chapter_number"])
    blueprint_data["chapter_outline"] = normalized


def _validate_complete_chapter_outline(blueprint_data: Dict[str, Any], target_chapters: Optional[int]) -> None:
    outlines = blueprint_data.get("chapter_outline")
    if not isinstance(outlines, list) or not outlines:
        raise ValueError("chapter_outline 为空")

    if not target_chapters:
        return

    if len(outlines) != target_chapters:
        raise ValueError(f"chapter_outline 数量为 {len(outlines)}，不是目标 {target_chapters} 章")

    expected_numbers = list(range(1, target_chapters + 1))
    actual_numbers = [item.get("chapter_number") for item in outlines if isinstance(item, dict)]
    if actual_numbers != expected_numbers:
        raise ValueError("chapter_outline 章节号不连续或不是从 1 开始")


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _build_novel_qa_context(project: NovelProjectSchema) -> str:
    blueprint = project.blueprint
    if not blueprint:
        return json.dumps(
            {
                "title": project.title,
                "initial_prompt": project.initial_prompt,
                "note": "这个项目还没有完整蓝图。",
            },
            ensure_ascii=False,
        )

    chapter_outline = [
        {
            "chapter_number": item.chapter_number,
            "title": item.title,
            "summary": _truncate_text(item.summary, 260),
        }
        for item in (blueprint.chapter_outline or [])[:80]
    ]
    completed_chapters = [
        {
            "chapter_number": chapter.chapter_number,
            "title": chapter.title,
            "summary": _truncate_text(chapter.real_summary or chapter.summary, 300),
        }
        for chapter in (project.chapters or [])
        if chapter.content
    ][:40]

    context = {
        "title": project.title,
        "genre": blueprint.genre,
        "target_audience": blueprint.target_audience,
        "style": blueprint.style,
        "tone": blueprint.tone,
        "one_sentence_summary": blueprint.one_sentence_summary,
        "full_synopsis": _truncate_text(blueprint.full_synopsis, 1800),
        "world_setting": blueprint.world_setting,
        "characters": blueprint.characters[:30],
        "relationships": [item.model_dump() for item in blueprint.relationships[:40]],
        "chapter_outline": chapter_outline,
        "completed_chapters": completed_chapters,
    }
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def _parse_llm_json_object(raw: str) -> Dict[str, Any]:
    normalized = unwrap_markdown_json(remove_think_tags(raw))
    sanitized = sanitize_json_like_text(normalized)
    parsed = json.loads(sanitized)
    if not isinstance(parsed, dict):
        raise ValueError("AI 返回内容不是 JSON 对象")
    return parsed


def _build_outline_batch_prompt(
    blueprint_data: Dict[str, Any],
    existing_outlines: List[Dict[str, Any]],
    start_chapter: int,
    end_chapter: int,
    target_chapters: int,
) -> str:
    characters = blueprint_data.get("characters")
    relationships = blueprint_data.get("relationships")
    context = {
        "title": blueprint_data.get("title"),
        "genre": blueprint_data.get("genre"),
        "style": blueprint_data.get("style"),
        "tone": blueprint_data.get("tone"),
        "one_sentence_summary": blueprint_data.get("one_sentence_summary"),
        "full_synopsis": blueprint_data.get("full_synopsis"),
        "world_setting": blueprint_data.get("world_setting"),
        "characters": characters[:12] if isinstance(characters, list) else [],
        "relationships": relationships[:12] if isinstance(relationships, list) else [],
        "recent_outline": existing_outlines[-3:],
    }
    expected_count = end_chapter - start_chapter + 1
    return f"""
请根据以下小说蓝图主体，生成第 {start_chapter} 章到第 {end_chapter} 章的章节大纲。
整部小说总共 {target_chapters} 章，本批必须正好返回 {expected_count} 条。

蓝图主体：
{json.dumps(context, ensure_ascii=False, separators=(",", ":"))}

硬性要求：
1. 只返回合法 JSON 对象，格式为 {{"chapter_outline":[{{"chapter_number":{start_chapter},"title":"章节标题","summary":"章节概要"}}]}}。
2. chapter_number 必须从 {start_chapter} 连续到 {end_chapter}，不能缺章、跳章、重复章。
3. 每条 summary 要写清本章关键事件、冲突推进、人物变化和结尾钩子。
4. 不要返回整部蓝图，不要 Markdown，不要解释文字。
"""


async def _generate_outline_batch(
    llm_service: LLMService,
    project_id: str,
    user_id: int,
    blueprint_data: Dict[str, Any],
    existing_outlines: List[Dict[str, Any]],
    start_chapter: int,
    end_chapter: int,
    target_chapters: int,
) -> List[Dict[str, Any]]:
    prompt = _build_outline_batch_prompt(
        blueprint_data,
        existing_outlines,
        start_chapter,
        end_chapter,
        target_chapters,
    )
    attempt_errors: List[str] = []
    for attempt in range(1, BLUEPRINT_GENERATION_MAX_ATTEMPTS + 1):
        raw = ""
        try:
            raw = await llm_service.get_llm_response(
                system_prompt="你是小说结构策划师。必须严格返回合法 JSON 对象。",
                conversation_history=[{"role": "user", "content": prompt}],
                temperature=0.2,
                user_id=user_id,
                timeout=300.0,
            )
            parsed = _parse_llm_json_object(raw)
            batch_data = {"chapter_outline": parsed.get("chapter_outline")}
            _normalize_chapter_outline(batch_data)
            outlines = [
                item
                for item in batch_data["chapter_outline"]
                if start_chapter <= item["chapter_number"] <= end_chapter
            ]
            expected_numbers = list(range(start_chapter, end_chapter + 1))
            actual_numbers = [item["chapter_number"] for item in outlines]
            if actual_numbers != expected_numbers:
                raise ValueError(f"章节号应为 {expected_numbers}，实际为 {actual_numbers}")
            if any(not item["title"].strip() or not item["summary"].strip() for item in outlines):
                raise ValueError("章节标题或概要为空")
            return outlines
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            attempt_errors.append(str(exc))
            logger.warning(
                "项目 %s 章节大纲 %s-%s 生成第 %s/%s 次失败：%s\n原始响应: %s",
                project_id,
                start_chapter,
                end_chapter,
                attempt,
                BLUEPRINT_GENERATION_MAX_ATTEMPTS,
                exc,
                raw[:500],
            )
        except HTTPException as exc:
            if exc.status_code < 500:
                raise
            attempt_errors.append(str(exc.detail))
            logger.warning(
                "项目 %s 章节大纲 %s-%s 生成第 %s/%s 次 AI 服务异常，准备重试：%s",
                project_id,
                start_chapter,
                end_chapter,
                attempt,
                BLUEPRINT_GENERATION_MAX_ATTEMPTS,
                exc.detail,
            )

    raise ValueError(
        f"第 {start_chapter}-{end_chapter} 章大纲生成失败，最近错误: "
        f"{attempt_errors[-1] if attempt_errors else '未知错误'}"
    )


async def _complete_chapter_outline(
    llm_service: LLMService,
    project_id: str,
    user_id: int,
    blueprint_data: Dict[str, Any],
    target_chapters: int,
) -> None:
    completed: List[Dict[str, Any]] = []
    for start_chapter in range(1, target_chapters + 1, BLUEPRINT_OUTLINE_BATCH_SIZE):
        end_chapter = min(start_chapter + BLUEPRINT_OUTLINE_BATCH_SIZE - 1, target_chapters)
        batch = await _generate_outline_batch(
            llm_service,
            project_id,
            user_id,
            blueprint_data,
            completed,
            start_chapter,
            end_chapter,
            target_chapters,
        )
        completed.extend(batch)

    blueprint_data["chapter_outline"] = completed
    _validate_complete_chapter_outline(blueprint_data, target_chapters)


@router.post("", response_model=NovelProjectSchema, status_code=status.HTTP_201_CREATED)
async def create_novel(
    title: str = Body(...),
    initial_prompt: str = Body(...),
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> NovelProjectSchema:
    """为当前用户创建一个新的小说项目。"""
    novel_service = NovelService(session)
    project = await novel_service.create_project(current_user.id, title, initial_prompt)
    logger.info("用户 %s 创建项目 %s", current_user.id, project.id)
    return await novel_service.get_project_schema(project.id, current_user.id)


@router.post("/import", response_model=Dict[str, str], status_code=status.HTTP_201_CREATED)
async def import_novel(
    file: UploadFile,
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> Dict[str, str]:
    """上传并导入小说文件。"""
    import_service = ImportService(session)
    project_id = await import_service.import_novel_from_file(current_user.id, file)
    logger.info("用户 %s 导入项目 %s", current_user.id, project_id)
    return {"id": project_id}


@router.get("", response_model=List[NovelProjectSummary])
async def list_novels(
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> List[NovelProjectSummary]:
    """列出用户的全部小说项目摘要信息。"""
    novel_service = NovelService(session)
    projects = await novel_service.list_projects_for_user(current_user.id)
    logger.info("用户 %s 获取项目列表，共 %s 个", current_user.id, len(projects))
    return projects


@router.get("/{project_id}", response_model=NovelProjectSchema)
async def get_novel(
    project_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> NovelProjectSchema:
    novel_service = NovelService(session)
    logger.info("用户 %s 查询项目 %s", current_user.id, project_id)
    return await novel_service.get_project_schema(project_id, current_user.id)


@router.post("/{project_id}/qa", response_model=NovelQAResponse)
async def ask_novel_question(
    project_id: str,
    request: NovelQARequest,
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> NovelQAResponse:
    novel_service = NovelService(session)
    llm_service = LLMService(session)

    project = await novel_service.get_project_schema(project_id, current_user.id)
    context = _build_novel_qa_context(project)
    system_prompt = """
你是当前小说项目的 AI 顾问。你只能根据提供的小说上下文回答问题。
回答要具体、清楚、有条理，优先引用已有蓝图、世界观、人物关系、章节大纲和已完成章节摘要。
如果上下文里没有答案，请直接说明“当前资料里还没有明确设定”，并给出可补充的方向。
不要编造已经发生的剧情；如果是创作建议，请明确标注为“建议”。
"""
    user_prompt = f"""
[小说上下文]
{context}

[用户问题]
{request.question.strip()}
"""
    answer = await llm_service.get_llm_response(
        system_prompt=system_prompt,
        conversation_history=[{"role": "user", "content": user_prompt}],
        temperature=0.35,
        user_id=current_user.id,
        timeout=180.0,
        response_format=None,
    )
    return NovelQAResponse(answer=remove_think_tags(answer).strip())


@router.get("/{project_id}/sections/{section}", response_model=NovelSectionResponse)
async def get_novel_section(
    project_id: str,
    section: NovelSectionType,
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> NovelSectionResponse:
    novel_service = NovelService(session)
    logger.info("用户 %s 获取项目 %s 的 %s 区段", current_user.id, project_id, section)
    return await novel_service.get_section_data(project_id, current_user.id, section)


@router.get("/{project_id}/chapters/{chapter_number}", response_model=ChapterSchema)
async def get_chapter(
    project_id: str,
    chapter_number: int,
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> ChapterSchema:
    novel_service = NovelService(session)
    logger.info("用户 %s 获取项目 %s 第 %s 章", current_user.id, project_id, chapter_number)
    return await novel_service.get_chapter_schema(project_id, current_user.id, chapter_number)


@router.delete("", status_code=status.HTTP_200_OK)
async def delete_novels(
    project_ids: List[str] = Body(...),
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> Dict[str, str]:
    novel_service = NovelService(session)
    await novel_service.delete_projects(project_ids, current_user.id)
    logger.info("用户 %s 删除项目 %s", current_user.id, project_ids)
    return {"status": "success", "message": f"成功删除 {len(project_ids)} 个项目"}


@router.post("/{project_id}/concept/converse", response_model=ConverseResponse)
async def converse_with_concept(
    project_id: str,
    request: ConverseRequest,
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> ConverseResponse:
    """与概念设计师（LLM）进行对话，引导蓝图筹备。"""
    novel_service = NovelService(session)
    prompt_service = PromptService(session)
    llm_service = LLMService(session)

    project = await novel_service.ensure_project_owner(project_id, current_user.id)

    history_records = await novel_service.list_conversations(project_id)
    logger.info(
        "项目 %s 概念对话请求，用户 %s，历史记录 %s 条",
        project_id,
        current_user.id,
        len(history_records),
    )
    conversation_history = [
        {"role": record.role, "content": record.content}
        for record in history_records
    ]
    user_content = json.dumps(request.user_input, ensure_ascii=False)
    conversation_history.append({"role": "user", "content": user_content})

    system_prompt = _ensure_prompt(await prompt_service.get_prompt("concept"), "concept")
    system_prompt = f"{system_prompt}\n{JSON_RESPONSE_INSTRUCTION}"

    llm_response = await llm_service.get_llm_response(
        system_prompt=system_prompt,
        conversation_history=conversation_history,
        temperature=0.8,
        user_id=current_user.id,
        timeout=240.0,
    )
    llm_response = remove_think_tags(llm_response)

    try:
        normalized = unwrap_markdown_json(llm_response)
        sanitized = sanitize_json_like_text(normalized)
        parsed = json.loads(sanitized)
    except json.JSONDecodeError as exc:
        logger.exception(
            "Failed to parse concept converse response: project_id=%s user_id=%s error=%s\nOriginal response: %s\nNormalized: %s\nSanitized: %s",
            project_id,
            current_user.id,
            exc,
            llm_response[:1000],
            normalized[:1000] if 'normalized' in locals() else "N/A",
            sanitized[:1000] if 'sanitized' in locals() else "N/A",
        )
        raise HTTPException(
            status_code=500,
            detail=f"概念对话失败，AI 返回的内容格式不正确。请重试或联系管理员。错误详情: {str(exc)}"
        ) from exc

    await novel_service.append_conversation(project_id, "user", user_content)
    await novel_service.append_conversation(project_id, "assistant", normalized)

    logger.info("项目 %s 概念对话完成，is_complete=%s", project_id, parsed.get("is_complete"))

    if parsed.get("is_complete"):
        parsed["ready_for_blueprint"] = True

    parsed.setdefault("conversation_state", parsed.get("conversation_state", {}))
    return ConverseResponse(**parsed)


@router.post("/{project_id}/blueprint/generate", response_model=BlueprintGenerationResponse)
async def generate_blueprint(
    project_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> BlueprintGenerationResponse:
    """根据完整对话生成可执行的小说蓝图。"""
    novel_service = NovelService(session)
    prompt_service = PromptService(session)
    llm_service = LLMService(session)

    project = await novel_service.ensure_project_owner(project_id, current_user.id)
    logger.info("项目 %s 开始生成蓝图", project_id)

    history_records = await novel_service.list_conversations(project_id)
    if not history_records:
        logger.warning("项目 %s 缺少对话历史，无法生成蓝图", project_id)
        raise HTTPException(status_code=400, detail="缺少对话历史，请先完成概念对话后再生成蓝图")

    formatted_history: List[Dict[str, str]] = []
    for record in history_records:
        role = record.role
        content = record.content
        if not role or not content:
            continue
        try:
            normalized = unwrap_markdown_json(content)
            data = json.loads(normalized)
            if role == "user":
                user_value = data.get("value", data)
                if isinstance(user_value, str):
                    formatted_history.append({"role": "user", "content": user_value})
            elif role == "assistant":
                ai_message = data.get("ai_message") if isinstance(data, dict) else None
                if ai_message:
                    formatted_history.append({"role": "assistant", "content": ai_message})
        except (json.JSONDecodeError, AttributeError):
            continue

    if not formatted_history:
        logger.warning("项目 %s 对话历史格式异常，无法提取有效内容", project_id)
        raise HTTPException(
            status_code=400,
            detail="无法从历史对话中提取有效内容，请检查对话历史格式或重新进行概念对话"
        )

    target_chapters = _extract_target_chapter_count(formatted_history) or DEFAULT_BLUEPRINT_CHAPTERS
    constraints = _build_blueprint_constraints(target_chapters)
    system_prompt = f"{_build_blueprint_core_prompt(target_chapters)}\n{constraints}"
    constrained_history = [
        *formatted_history,
        {
            "role": "user",
            "content": (
                f"请现在生成最终小说蓝图主体。{constraints}"
                "只返回合法 JSON，不要输出 Markdown。"
            ),
        },
    ]

    blueprint_data: Dict[str, Any] | None = None
    attempt_errors: List[str] = []
    for attempt in range(1, BLUEPRINT_GENERATION_MAX_ATTEMPTS + 1):
        blueprint_raw = ""
        try:
            blueprint_raw = await llm_service.get_llm_response(
                system_prompt=system_prompt,
                conversation_history=constrained_history,
                temperature=0.25,
                user_id=current_user.id,
                timeout=480.0,
            )
            candidate = _parse_llm_json_object(blueprint_raw)
            candidate["chapter_outline"] = []
            Blueprint(**candidate)
            blueprint_data = candidate
            break
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            message = str(exc)
            attempt_errors.append(message)
            logger.warning(
                "项目 %s 蓝图主体生成第 %s/%s 次失败，准备重试：%s\n原始响应: %s",
                project_id,
                attempt,
                BLUEPRINT_GENERATION_MAX_ATTEMPTS,
                message,
                blueprint_raw[:500],
            )
        except HTTPException as exc:
            if exc.status_code < 500:
                raise
            message = str(exc.detail)
            attempt_errors.append(message)
            logger.warning(
                "项目 %s 蓝图主体生成第 %s/%s 次 AI 服务异常，准备重试：%s",
                project_id,
                attempt,
                BLUEPRINT_GENERATION_MAX_ATTEMPTS,
                message,
            )

    if blueprint_data is None:
        logger.error(
            "项目 %s 蓝图生成连续 %s 次失败：%s",
            project_id,
            BLUEPRINT_GENERATION_MAX_ATTEMPTS,
            " | ".join(attempt_errors),
        )
        raise HTTPException(
            status_code=500,
            detail=(
                f"蓝图主体生成失败，已自动重试 {BLUEPRINT_GENERATION_MAX_ATTEMPTS} 次仍未生成可用蓝图。"
                f"最近错误: {attempt_errors[-1] if attempt_errors else '未知错误'}"
            ),
        )

    try:
        await _complete_chapter_outline(
            llm_service,
            project_id,
            current_user.id,
            blueprint_data,
            target_chapters,
        )
    except ValueError as exc:
        logger.error("项目 %s 完整章节大纲生成失败：%s", project_id, exc)
        raise HTTPException(
            status_code=500,
            detail=f"章节大纲生成失败，已自动多次重试。错误详情: {exc}",
        ) from exc

    blueprint = Blueprint(**blueprint_data)
    await novel_service.replace_blueprint(project_id, blueprint)
    if blueprint.title:
        project.title = blueprint.title
        project.status = "blueprint_ready"
        await session.commit()
        logger.info("项目 %s 更新标题为 %s，并标记为 blueprint_ready", project_id, blueprint.title)

    ai_message = (
        "太棒了！我已经根据我们的对话整理出完整的小说蓝图。请确认是否进入写作阶段，或提出修改意见。"
    )
    return BlueprintGenerationResponse(blueprint=blueprint, ai_message=ai_message)


@router.post("/{project_id}/blueprint/save", response_model=NovelProjectSchema)
async def save_blueprint(
    project_id: str,
    blueprint_data: Blueprint | None = Body(None),
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> NovelProjectSchema:
    """保存蓝图信息，可用于手动覆盖自动生成结果。"""
    novel_service = NovelService(session)
    project = await novel_service.ensure_project_owner(project_id, current_user.id)

    if blueprint_data:
        await novel_service.replace_blueprint(project_id, blueprint_data)
        if blueprint_data.title:
            project.title = blueprint_data.title
            await session.commit()
        logger.info("项目 %s 手动保存蓝图", project_id)
    else:
        logger.warning("项目 %s 保存蓝图时未提供蓝图数据", project_id)
        raise HTTPException(status_code=400, detail="缺少蓝图数据，请提供有效的蓝图内容")

    return await novel_service.get_project_schema(project_id, current_user.id)


@router.patch("/{project_id}/blueprint", response_model=NovelProjectSchema)
async def patch_blueprint(
    project_id: str,
    payload: BlueprintPatch,
    session: AsyncSession = Depends(get_session),
    current_user: UserInDB = Depends(get_current_user),
) -> NovelProjectSchema:
    """局部更新蓝图字段，对世界观或角色做微调。"""
    novel_service = NovelService(session)
    project = await novel_service.ensure_project_owner(project_id, current_user.id)

    update_data = payload.model_dump(exclude_unset=True)
    await novel_service.patch_blueprint(project_id, update_data)
    logger.info("项目 %s 局部更新蓝图字段：%s", project_id, list(update_data.keys()))
    return await novel_service.get_project_schema(project_id, current_user.id)
