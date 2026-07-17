# Prompt 配置管理面运维说明

> 设计依据:`docs/Memory 系统方案设计 —— 分层认知记忆架构.md` §2.8.4.1
> 落地阶段:**Phase 0 Step 0.7**

LLM Prompt 与插件配置共用 ConfigCenter。本文给出运维侧的 curl 示例与
match 维度速查。

---

## 1. 端点速查

| 方法   | 路径                                | 用途                            |
| ------ | ----------------------------------- | ------------------------------- |
| GET    | /v1/admin/prompts                   | 列出所有 prompt_id              |
| GET    | /v1/admin/prompts/{id}              | 获取 prompt(可预览灰度命中)     |
| GET    | /v1/admin/prompts/{id}/history      | 历史(File 后端进程内 200 条)    |
| PUT    | /v1/admin/prompts/{id}              | 写入 / 更新(含 variants)        |
| DELETE | /v1/admin/prompts/{id}              | 删除(占位 placeholder)          |
| POST   | /v1/admin/prompts/{id}/render       | 试渲染验证灰度 + 变量代入       |

鉴权:与 `/v1/admin/plugins` 一致 —— `bootstrap.yaml` 的
`admin_api_key` 非空时要求 `X-Admin-Key`。

---

## 2. 内置 prompt_id 清单

| prompt_id                          | 用途              | 消费方       |
| ---------------------------------- | ----------------- | ------------ |
| `episode_extraction`               | 个人情景提取      | Step 3.2     |
| `episode_extraction_group_chat`    | 群组情景提取      | Step 3.2     |
| `semantic_extraction`              | 语义联想提取      | Step 3.3     |
| `sbd_llm_refine`                   | SBD LLM 边界细化  | Step 3.1     |
| `user_preference_extract`          | 用户偏好(预留)    | Phase 5+     |

> ⚠️ 内置种子在代码中(`memory_app/prompt_manager/builtins.py`)。即便
> 运维误删 `default.yaml` 的 `memory.prompts.*` 段,业务仍能跑出
> `source=builtin` 的回退结果。

---

## 3. 灰度 match 维度

与插件灰度共用 5 维(详见 `docs/.../§2.8.4`),全部 AND 关系:

| 维度                       | 语义                                         | 示例                                        |
| -------------------------- | -------------------------------------------- | ------------------------------------------- |
| `tenant_id_in`             | 租户白名单                                   | `["acme", "globex"]`                        |
| `user_id_hash_mod_100_lt`  | 前 N% 用户(MD5 哈希)                         | `10` 表示前 10%                             |
| `traffic_pct`              | 流量百分比(基于 user_id 稳定分桶)             | `5` 表示 5%                                 |
| `time_range`               | 时间窗(ISO8601)                              | `["2026-01-01T00:00:00Z", "2026-01-31..."]` |
| `tag_in`                   | 自定义标签集合                               | `["beta_user"]`                             |

命中后,resolved 响应 `source=variant`。

---

## 4. curl 烟测

```bash
# 4.1 列出所有 prompt_id(默认含内置种子)
curl -s 'http://127.0.0.1:8000/v1/admin/prompts' | python3 -m json.tool

# 4.2 获取默认 episode_extraction(无租户上下文)
curl -s 'http://127.0.0.1:8000/v1/admin/prompts/episode_extraction' \
  | python3 -m json.tool
# 预期 source = "default"(命中 default.yaml)

# 4.3 预览 acme 租户的灰度
curl -s 'http://127.0.0.1:8000/v1/admin/prompts/episode_extraction?tenant_id=acme' \
  | python3 -m json.tool
# 预期 source = "variant",template 含 "Acme 企业助手"

# 4.4 写入 / 更新(含 variants)
curl -s -X PUT 'http://127.0.0.1:8000/v1/admin/prompts/semantic_extraction' \
  -H 'Content-Type: application/json' \
  -d '{
        "template": "新版 - 从 {summary} 提炼...",
        "variables": ["summary", "entities"],
        "description": "Phase 5 试用版",
        "version": "1.1.0",
        "variants": [
          {
            "match": {"tenant_id_in": ["acme"]},
            "template": "Acme 版 - 从 {summary} 提炼..."
          }
        ]
      }' | python3 -m json.tool
# 预期 {"prompt_id":"semantic_extraction","version":N,"scope":"global",...}

# 4.5 历史
curl -s 'http://127.0.0.1:8000/v1/admin/prompts/semantic_extraction/history?limit=5' \
  | python3 -m json.tool

# 4.6 试渲染(校验 variables 代入 + 灰度命中)
curl -s -X POST 'http://127.0.0.1:8000/v1/admin/prompts/episode_extraction/render?tenant_id=acme' \
  -H 'Content-Type: application/json' \
  -d '{"variables": {"text": "我下周要去北京"}}' \
  | python3 -m json.tool
# 预期 rendered 字段是渲染后的字符串,source=variant

# 4.7 删除(Phase 0 占位 placeholder,Phase 8.3 改真删)
curl -s -X DELETE 'http://127.0.0.1:8000/v1/admin/prompts/semantic_extraction?scope=global' \
  | python3 -m json.tool
```

> 鉴权开启时所有 curl 需加 `-H 'X-Admin-Key: <your_key>'`。

---

## 5. PUT body 字段

| 字段          | 必填 | 说明                                                     |
| ------------- | ---- | -------------------------------------------------------- |
| `template`    | ✅   | Python `str.format` 模板;JSON 花括号转义为 `{{` / `}}`   |
| `variables`   | ⛔   | 占位符名列表(用于 render 校验)                           |
| `description` | ⛔   | 元数据                                                   |
| `version`     | ⛔   | 元数据                                                   |
| `tags`        | ⛔   | 字符串列表                                               |
| `variants[]`  | ⛔   | 每条含 `match`(dict)+ 覆盖字段(顶层 `template` 等)        |

校验失败返回 HTTP 400,`detail` 含 `json_pointer` 与 `message`。

---

## 6. File / Mongo 后端差异

| 维度          | File 后端                              | Mongo 后端                              |
| ------------- | -------------------------------------- | --------------------------------------- |
| 历史持久化    | 进程内环形缓冲(~200 条),重启丢失       | `global_config_history` collection 持久化 |
| 写入回写      | 改写 `config/default.yaml`             | `global_config` upsert                  |
| 变更监听      | mtime 轮询(默认 1s)                    | TTL 缓存兜底(Phase 8 接 Change Stream)    |
| 多副本一致性  | ❌ 仅本副本生效                          | ✅ 所有副本通过 Mongo 共享                |
| 适用阶段      | 开发态默认                             | 生产态(Phase 8 Step 8.2 完整化)          |

---

## 7. 业务侧消费规范

```python
from memory_app.prompt_runtime import get_prompt_manager

# Phase 3+ 提取器内部:
prompt = await get_prompt_manager().render_for(
    "episode_extraction",
    tenant_id=memcell.tenant_id,
    user_id=memcell.user_id,
    text=memcell.text,
)
```

**禁止**在 `extractors/*.py` / `sbd.py` 内定义
`EPISODE_EXTRACTION_PROMPT` 类常量 —— Step 8.1 的
`scripts/audit_no_hard_deps.py` 会在 CI 中拦截。

---

## 8. 故障排查

| 现象                                     | 排查                                                       |
| ---------------------------------------- | ---------------------------------------------------------- |
| GET 返回 `source=builtin`                | `default.yaml` 中无该 prompt_id;运维补全或采用内置        |
| `?tenant_id=acme` 但 source 仍 `default` | 检查 variants `match.tenant_id_in` 是否含 "acme"           |
| PUT 返回 400 `template required`         | body 中 template 字段为空或非字符串                        |
| render 返回 400 `missing prompt vars`    | `variables` 字段与模板占位符不匹配                         |
| File 后端历史 < 预期                     | 进程重启过(File 后端历史进程内,生产用 Mongo 后端)          |
