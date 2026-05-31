"""``/v1/admin/*`` 管理面 API。

═══════════════════════════════════════════════════════════════════════════════
端点(脚手架 + 管理面 完整化)
═══════════════════════════════════════════════════════════════════════════════

| 方法   | 路径                                                 | 阶段   |
| ------ | ---------------------------------------------------- | ------ |
| GET    | /v1/admin/plugins                                    | P0     |
| GET    | /v1/admin/plugins/health                             | P0     |
| GET    | /v1/admin/plugins/{category}/{name}/health           | P8.3   |
| POST   | /v1/admin/plugins/{category}/{name}/reload           | P8.3   |
| GET    | /v1/admin/prompts                                    | P0.7   |
| GET    | /v1/admin/prompts/{id}                               | P0.7   |
| GET    | /v1/admin/prompts/{id}/history                       | P0.7   |
| PUT    | /v1/admin/prompts/{id}                               | P0.7   |
| DELETE | /v1/admin/prompts/{id}                               | P0.7   |
| POST   | /v1/admin/prompts/{id}/render                        | P0.7   |
| GET    | /v1/admin/config?category=...&tenant_id=...          | P8.3   |
| POST   | /v1/admin/config                                     | P8.3   |
| GET    | /v1/admin/config/history?category=...&limit=50       | P8.3   |
| POST   | /v1/admin/config/rollback                            | P8.3   |

═══════════════════════════════════════════════════════════════════════════════
鉴权策略
═══════════════════════════════════════════════════════════════════════════════
两道闸:

1. **总开关** ``settings.auth_enabled`` —— 关闭时业务面 ``/v1/memory/*``、``/v1/query/*`` 自由访问；
   开启时要求 ``X-API-Key`` 或 ``X-Admin-Key``（值均为 ``admin_api_key``）。
2. **API Key** ``settings.admin_api_key`` —— 配置后 **管理面始终** 要求 ``X-Admin-Key`` 匹配
   （即使 ``auth_enabled=false``，防止误配 key 仍暴露管理面）。
   未配置 key 且 ``auth_enabled=true`` 时管理面直接 403。

生产环境应在网关层再加 IP 白名单作为第三道防护。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from memory_app.config_center import (
    ConfigValidationError,
    PromptNotFoundError,
)
from memory_app.deps.state import app_state
from memory_app.security import verify_admin_key
from memory_app.plugins import registry as plugin_registry
from memory_app.prompt_manager.manager import _format_template

logger = logging.getLogger(__name__)

#: 路由前缀 ``/v1/admin``
router = APIRouter(prefix="/v1/admin", tags=["admin"])


# ════════════════════════════════════════════════════════════════════════════
# 鉴权
# ════════════════════════════════════════════════════════════════════════════
def _check_admin_key(x_admin_key: str | None) -> None:
    """校验 X-Admin-Key 头。规则见模块 docstring。

    :raises HTTPException: 403 当鉴权失败
    """
    settings = app_state.settings
    if settings is None:
        return
    verify_admin_key(x_admin_key, settings)


def _require_config_center():
    """确保 ConfigCenter 已就绪;否则返回 503。"""
    cc = app_state.config_center
    if cc is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="config_center not initialized"
        )
    return cc


# ════════════════════════════════════════════════════════════════════════════
# 插件管理面
# ════════════════════════════════════════════════════════════════════════════
@router.get("/plugins")
async def list_plugins(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """列出所有已注册插件 + 当前活动实例。

    管理面 完整化时将增补:``/{category}/{name}/reload`` 与
    ``/{category}/{name}/health``。
    """
    _check_admin_key(x_admin_key)
    return {
        # registry.describe 给出 "类层"快照(已注册的所有类)
        "categories": plugin_registry.describe(),
        # plugin_factory.list_active 给出 "实例层"快照(已 start 的实例)
        "active": app_state.plugin_factory.list_active() if app_state.plugin_factory else [],
    }


@router.get("/plugins/health")
async def plugins_health(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """聚合所有活动插件实例的 ``health`` 输出。

    与 ``/health/ready`` 的区别:本端点报告**插件实例级**健康,``/health/ready``
    报告**外部依赖级**健康;二者互补。
    """
    _check_admin_key(x_admin_key)
    if app_state.plugin_factory is None:
        return {}
    return await app_state.plugin_factory.healthcheck_all()


# ════════════════════════════════════════════════════════════════════════════
# Prompt 管理面( / )
# ════════════════════════════════════════════════════════════════════════════
@router.get("/prompts")
async def list_prompts(
    tag: str | None = Query(default=None, description="按 tags 过滤"),
    include_builtin: bool = Query(default=True, description="是否合并内置种子"),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """列出所有可见 prompt_id。"""
    _check_admin_key(x_admin_key)
    cc = _require_config_center()
    ids = await cc.list_prompt_ids(include_builtin=include_builtin, tag=tag)  # type: ignore[attr-defined]
    return {"prompts": ids, "total": len(ids)}


@router.get("/prompts/{prompt_id}")
async def get_prompt(
    prompt_id: str,
    tenant_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """获取 prompt 当前有效配置。

    带 ``?tenant_id=&user_id=`` 时按五级覆盖 + 灰度规则解析,响应 ``source``
    字段标明命中层(default / global / tenant / user / variant / builtin)。
    """
    _check_admin_key(x_admin_key)
    cc = _require_config_center()
    try:
        resolved = await cc.resolve_prompt(  # type: ignore[attr-defined]
            prompt_id, tenant_id=tenant_id, user_id=user_id
        )
    except PromptNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"prompt not found: {prompt_id}"
        )
    return resolved.model_dump()


@router.get("/prompts/{prompt_id}/history")
async def get_prompt_history(
    prompt_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """返回 prompt 历史(最新优先)。

    File 后端在进程内环形缓冲(约 200 条,重启丢失);Mongo 后端持久化。
    """
    _check_admin_key(x_admin_key)
    cc = _require_config_center()
    history = await cc.history_prompt(prompt_id, limit=limit)  # type: ignore[attr-defined]
    return {"prompt_id": prompt_id, "history": history, "count": len(history)}


@router.put("/prompts/{prompt_id}")
async def write_prompt(
    prompt_id: str,
    body: dict[str, Any] = Body(...),
    scope: str = Query(default="global", pattern="^(global|tenant|user)$"),
    scope_id: str | None = Query(default=None),
    actor: str = Query(default="ops"),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """写入 / 更新 prompt 覆盖。

    ``body`` 字段:

    - ``template``     必填,Python ``str.format`` 模板
    - ``variables``    可选,占位符列表
    - ``description`` / ``version`` / ``tags``  可选元数据
    - ``variants[]``   可选,灰度变体(每条含 ``match`` + 覆盖字段)

    ``scope=tenant|user`` 时必须提供 ``scope_id``。
    """
    _check_admin_key(x_admin_key)
    cc = _require_config_center()
    try:
        version = await cc.write_prompt(  # type: ignore[attr-defined]
            prompt_id, body, scope=scope, scope_id=scope_id, actor=actor
        )
    except ConfigValidationError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"json_pointer": e.json_pointer, "message": e.message},
        )
    except ValueError as e:
        # scope / scope_id 不一致触发
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {
        "prompt_id": prompt_id,
        "version": version,
        "scope": scope,
        "scope_id": scope_id,
    }


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(
    prompt_id: str,
    scope: str = Query(default="global", pattern="^(global|tenant|user)$"),
    scope_id: str | None = Query(default=None),
    actor: str = Query(default="ops"),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """清除指定 scope 下的 prompt 覆盖。

    脚手架 实现:写入一个 placeholder template 标记为"已删除"——后续
    resolve 仍能拿到,但 template 是 ``<<DELETED>>``,业务方据此识别。
    管理面 引入 ``_delete_entry`` hook 后会改为真正物理删除。
    """
    _check_admin_key(x_admin_key)
    cc = _require_config_center()
    placeholder_body = {
        "template": "<<DELETED>>",
        "variables": [],
        "description": f"deleted by {actor}",
        "tags": ["__deleted__"],
    }
    try:
        version = await cc.write_prompt(  # type: ignore[attr-defined]
            prompt_id, placeholder_body, scope=scope, scope_id=scope_id, actor=actor
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {
        "prompt_id": prompt_id,
        "version": version,
        "scope": scope,
        "scope_id": scope_id,
        "deleted": True,
        "note": "marked as deleted via placeholder; full delete in 管理面",
    }


@router.post("/prompts/{prompt_id}/render")
async def render_prompt(
    prompt_id: str,
    payload: dict[str, Any] = Body(...),
    tenant_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """试渲染 prompt 以验证灰度命中与变量代入。

    ``payload`` 形如 ``{"variables": {"text": "我下周去北京"}}``。
    """
    _check_admin_key(x_admin_key)
    cc = _require_config_center()
    # 关键:用 is None 区分"未传"与"传了非 dict";旧版 `or {}` 让任何
    # falsy 非 dict 值(``[]`` / ``""`` / ``0``)被无声转成空 dict,
    # 422 校验形同虚设,占位符未填会在 _format_template 抛 ValueError 400 才暴露。
    variables = payload.get("variables")
    if variables is None:
        variables = {}
    elif not isinstance(variables, dict):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="variables must be a mapping"
        )

    try:
        resolved = await cc.resolve_prompt(  # type: ignore[attr-defined]
            prompt_id, tenant_id=tenant_id, user_id=user_id
        )
    except PromptNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"prompt not found: {prompt_id}"
        )

    try:
        rendered = _format_template(resolved.template, resolved.variables, variables)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))

    return {
        "prompt_id": prompt_id,
        "source": resolved.source,
        "config_version": resolved.config_version,
        "rendered": rendered,
        "template": resolved.template,
        "variables": resolved.variables,
    }


# ════════════════════════════════════════════════════════════════════════════
# 管理面:单插件 health / reload
# ════════════════════════════════════════════════════════════════════════════
@router.get("/plugins/{category}/{name}/health")
async def plugin_health(
    category: str,
    name: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """单个插件实例健康检查。

    若该 ``(category, name)`` 当前无活动实例,返回 ``status="not_active"``;
    实例 ``health`` 抛错时返回 ``status="fail"``。
    """
    _check_admin_key(x_admin_key)
    if app_state.plugin_factory is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="plugin_factory not initialized"
        )
    return {
        "category": category,
        "name": name,
        **(await app_state.plugin_factory.health_of(category, name)),
    }


@router.post("/plugins/{category}/{name}/reload")
async def plugin_reload(
    category: str,
    name: str,
    actor: str = Query(default="ops"),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """手工触发指定插件重载(stop+丢弃 + 下次 build 重建)。

    用途:
    - 配置中心暂时不可达时的兜底(配置已写但 watcher 没能推送)
    - 灰度回滚后强制刷新

    返回 ``released_count`` 即被释放的实例数。
    """
    _check_admin_key(x_admin_key)
    if app_state.plugin_factory is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="plugin_factory not initialized"
        )
    released = await app_state.plugin_factory.release_category(category, name)
    logger.info(
        "manual reload by %s: %s/%s released_count=%d", actor, category, name, released
    )
    return {
        "category": category,
        "name": name,
        "released_count": released,
        "actor": actor,
    }


# ════════════════════════════════════════════════════════════════════════════
# 管理面:配置 CRUD
# ════════════════════════════════════════════════════════════════════════════
@router.get("/config")
async def get_config(
    category: str = Query(..., min_length=1),
    tenant_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """读取 ``category`` 当前生效配置(经五级覆盖 + 灰度路由解析)。

    响应字段 ``source`` 标明命中层(default / global / tenant / user / variant),
    便于运维排查"为什么这个用户走了 hybrid_sbd"。
    """
    _check_admin_key(x_admin_key)
    cc = _require_config_center()
    try:
        resolved = await cc.resolve(category, tenant_id=tenant_id, user_id=user_id)
    except LookupError as e:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"category not found: {e}"
        )
    return {
        "category": category,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "name": resolved.name,
        "params": resolved.params,
        "version": resolved.version,
        "source": resolved.source,
    }


class ConfigWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    scope: str = Field(default="global", pattern="^(global|tenant|user)$")
    scope_id: str | None = None
    actor: str = Field(default="ops")
    gray_rules: list[dict[str, Any]] | None = None


@router.post("/config")
async def write_config(
    body: ConfigWriteRequest = Body(...),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """写入配置(schema 校验 + 版本自增 + 历史保留)。

    ``scope=tenant|user`` 时必须提供 ``scope_id``;``gray_rules`` 为可选灰度
    变体列表,每条形如 ``{"match": {...}, "name"/"params": ...}``。
    """
    _check_admin_key(x_admin_key)
    cc = _require_config_center()
    try:
        version = await cc.write(
            body.category,
            body.name,
            body.params,
            scope=body.scope,
            scope_id=body.scope_id,
            actor=body.actor,
            gray_rules=body.gray_rules,
        )
    except ConfigValidationError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"json_pointer": e.json_pointer, "message": e.message},
        )
    except ValueError as e:
        # scope/scope_id 不一致时
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    logger.info(
        "config write by %s: %s name=%s scope=%s version=%d",
        body.actor, body.category, body.name, body.scope, version,
    )
    return {
        "category": body.category,
        "name": body.name,
        "scope": body.scope,
        "scope_id": body.scope_id,
        "version": version,
        "actor": body.actor,
    }


@router.get("/config/history")
async def get_config_history(
    category: str = Query(..., min_length=1),
    limit: int = Query(default=50, ge=1, le=500),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """返回 ``category`` 的历史版本(最新优先)。"""
    _check_admin_key(x_admin_key)
    cc = _require_config_center()
    try:
        history = await cc.history(category, limit=limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"history failed: {e.__class__.__name__}",
        )
    return {"category": category, "limit": limit, "history": history, "count": len(history)}


class ConfigRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(..., min_length=1)
    target_version: int = Field(..., ge=1)
    scope: str = Field(default="global", pattern="^(global|tenant|user)$")
    scope_id: str | None = None
    actor: str = Field(default="ops")


@router.post("/config/rollback")
async def rollback_config(
    body: ConfigRollbackRequest = Body(...),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """回滚到指定历史版本(产生一条新版本指向旧 ``name`` / ``params``)。

    实现策略:从 ``history`` 找到 ``target_version`` 的快照 → 当成新一次 ``write``
    重新落库,从而保留完整审计链。本端点**不**销毁现有数据,仅"前进式回退"。
    """
    _check_admin_key(x_admin_key)
    cc = _require_config_center()
    try:
        history = await cc.history(body.category, limit=500)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"history failed: {e.__class__.__name__}",
        )
    def _safe_version(h: dict) -> int:
        """history 行的 version 字段可能是 str / None / 异常值;
        转换失败一律视作 -1(永远不会等于合法的 target_version >= 0),
        让该行被无害跳过而不是把 admin 路由打成 500。"""
        try:
            return int(h.get("version", -1))
        except (TypeError, ValueError):
            return -1

    target = next(
        (
            h for h in history
            if _safe_version(h) == body.target_version
            and h.get("scope") == body.scope
            and h.get("scope_id") == body.scope_id
        ),
        None,
    )
    if target is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=(
                f"history not found: category={body.category} version={body.target_version}"
                f" scope={body.scope} scope_id={body.scope_id}"
            ),
        )
    try:
        new_version = await cc.write(
            body.category,
            target["name"],
            dict(target.get("params") or {}),
            scope=body.scope,
            scope_id=body.scope_id,
            actor=f"{body.actor}/rollback@v{body.target_version}",
            gray_rules=target.get("variants"),
        )
    except ConfigValidationError as e:
        # rollback 命中失效字段时仍按 422 暴露,运维可拉到具体哪个 key 失效
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"json_pointer": e.json_pointer, "message": e.message},
        )
    return {
        "category": body.category,
        "rolled_back_to": body.target_version,
        "new_version": new_version,
        "scope": body.scope,
        "scope_id": body.scope_id,
        "actor": body.actor,
    }
