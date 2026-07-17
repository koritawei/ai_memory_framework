"""健康检查端点（设计文档 §2.5）。

═══════════════════════════════════════════════════════════════════════════════
两个端点
═══════════════════════════════════════════════════════════════════════════════
- ``GET /health/live``    进程存活探测，**永远** 200。供 K8s ``livenessProbe``
- ``GET /health/ready``   就绪探测，聚合各依赖。供 K8s ``readinessProbe``

═══════════════════════════════════════════════════════════════════════════════
就绪状态判定
═══════════════════════════════════════════════════════════════════════════════
- 必启项（``config_center`` / ``plugin_registry``）任一失败 → ``status=fail``
- 默认（``strict_readiness=false``）：其他外部依赖（mongo/es/redis/milvus）失败
  → ``status=degraded``（K8s 仍认为 ready）
- 严格模式（``strict_readiness=true``）：任意失败 → ``status=fail``

degraded 而非 fail 的设计：让本服务在外部依赖暂时不可达时仍能启动并响应
管理面调用，运维有时间介入排查 —— 与设计文档 §5.4 服务降级策略一致。
"""

from __future__ import annotations

from fastapi import APIRouter

from memory_app.deps import app_state

#: 路由前缀 ``/health``，OpenAPI 标签 ``health``
router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness():
    """进程存活探测：永远返回 200。

    返回 ``{"status": "ok"}`` —— K8s ``livenessProbe`` 据此判定容器是否要重启。
    """
    return {"status": "ok"}


@router.get("/ready")
async def readiness():
    """就绪探测：聚合各依赖健康状态。

    - ``config_center`` / ``plugin_registry`` 必须 ok 才返回总状态 ok
    - 其他外部依赖（mongo/es/milvus/redis）失败时整体 status=degraded（开发态默认）
    - 当 ``settings.strict_readiness=True`` 时，任一外部依赖失败即返回 status=fail

    返回结构::

        {
          "status": "ok | degraded | fail",
          "checks": {
            "mongo":           {"status": "ok | fail", "detail": "..."},
            "es":              {"status": "...", "detail": "..."},
            "redis":           {"status": "...", "detail": "..."},
            "milvus":          {"status": "...", "detail": "..."},
            "config_center":   {"status": "ok",  "detail": "version=N, mtime=..."},
            "plugin_registry": {"status": "ok",  "detail": "categories=N"}
          }
        }
    """
    checks = await app_state.healthchecks()
    settings = app_state.settings
    strict = bool(settings and settings.strict_readiness)
    must_ok = ("config_center", "plugin_registry", "core_services")

    must_failed = [k for k in must_ok if checks.get(k, {}).get("status") != "ok"]
    other_failed = [
        k for k, v in checks.items() if k not in must_ok and v.get("status") != "ok"
    ]

    # 必启项失败始终 fail；其他失败按 strict 模式决定 degraded 还是 fail
    if must_failed:
        status = "fail"
    elif other_failed:
        status = "fail" if strict else "degraded"
    else:
        status = "ok"

    return {"status": status, "checks": checks}
