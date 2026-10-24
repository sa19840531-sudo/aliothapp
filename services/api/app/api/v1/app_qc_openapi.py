"""正式 App 质检业务 OpenAPI 与 Swagger 对接站。

对齐既有 mobile-demo /api-doc 门户风格，契约改为青云正式接口：
登录 → 上传 APK → 提交 App 任务 → 任务洞察 / 证据回读。
"""

from __future__ import annotations

from typing import Any

from services.api.app.api.v1.swagger_html import render_swagger_html

_APP_QC_DOC_VERSION = "app-qc-api-v1"

__all__ = [
    "APP_QC_DOC_VERSION",
    "build_app_qc_openapi",
    "render_app_qc_swagger_html",
]

APP_QC_DOC_VERSION = _APP_QC_DOC_VERSION


def build_app_qc_openapi(*, version: str = _APP_QC_DOC_VERSION) -> dict[str, Any]:
    """组装正式 App 质检对外 API OpenAPI 3.0。"""
    bearer = {
        "name": "Authorization",
        "in": "header",
        "required": True,
        "schema": {"type": "string"},
        "description": "登录后取得的 JWT：Bearer <access_token>",
    }
    task_id_param = {
        "name": "task_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }
    error_schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "code": {"type": "string"},
            "details": {"type": "object"},
        },
    }

    submit_app_task_body = {
        "type": "object",
        "required": ["target_platform", "ruleset_code", "task_config"],
        "properties": {
            "target_platform": {
                "type": "string",
                "enum": ["app"],
                "description": "App 质检固定为 app",
            },
            "target_url": {
                "type": "string",
                "nullable": True,
                "description": "App 任务传 null",
            },
            "ruleset_code": {
                "type": "string",
                "default": "alioth_first_ruleset",
            },
            "ruleset_version": {"type": "integer", "minimum": 1, "default": 1},
            "channel": {
                "type": "string",
                "enum": ["workbench", "telegram", "api"],
                "default": "workbench",
            },
            "priority": {"type": "integer", "minimum": 0, "default": 100},
            "task_config": {
                "type": "object",
                "required": ["task_name", "workflow_version", "app"],
                "properties": {
                    "task_name": {"type": "string"},
                    "workflow_version": {
                        "type": "string",
                        "enum": ["v2"],
                        "description": "App 质检锁定 v2",
                    },
                    "remark": {"type": "string", "nullable": True},
                    "app": {
                        "type": "object",
                        "required": ["platform_build_ref"],
                        "properties": {
                            "platform_build_ref": {
                                "type": "string",
                                "description": "上传 APK 返回值，格式 app-build:<uuid>",
                                "example": "app-build:11111111-2222-3333-4444-555555555555",
                            },
                            "package_name": {
                                "type": "string",
                                "nullable": True,
                                "description": "可选；缺省由制品解析",
                            },
                            "device": {
                                "type": "object",
                                "properties": {
                                    "backend": {
                                        "type": "string",
                                        "enum": [
                                            "cloud_phone",
                                            "ldplayer",
                                            "google_emulator",
                                            "redroid",
                                        ],
                                        "default": "cloud_phone",
                                    },
                                    "profile_id": {
                                        "type": "string",
                                        "default": "default",
                                    },
                                },
                            },
                        },
                    },
                    "product": {
                        "type": "object",
                        "properties": {
                            "department_name": {"type": "string"},
                            "product_code": {"type": "string"},
                            "product_name": {"type": "string"},
                        },
                    },
                    "journey_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选；有界 Journey 步骤提示",
                    },
                },
            },
            "page_sampling_policy": {"type": "object"},
            "loop_guard": {"type": "object"},
        },
        "example": {
            "target_platform": "app",
            "target_url": None,
            "ruleset_code": "alioth_first_ruleset",
            "ruleset_version": 1,
            "channel": "workbench",
            "priority": 100,
            "task_config": {
                "task_name": "示例 App 质检",
                "workflow_version": "v2",
                "remark": None,
                "app": {
                    "platform_build_ref": "app-build:11111111-2222-3333-4444-555555555555",
                    "package_name": None,
                    "device": {"backend": "cloud_phone", "profile_id": "default"},
                },
                "product": {
                    "department_name": "鼎丰",
                    "product_code": "app-qc",
                    "product_name": "App质检",
                },
            },
            "page_sampling_policy": {},
            "loop_guard": {},
        },
    }

    submit_task_response = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "format": "uuid"},
            "run_id": {"type": "string"},
            "ruleset_id": {"type": "string"},
            "status": {"type": "string"},
        },
    }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Alioth 正式 App 质检 API",
            "description": (
                "青云工作台正式 App 质检业务契约（非 mobile-demo 烟测）。\n\n"
                "**推荐闭环**：`POST /auth/login` → `POST /app-builds` → "
                "`POST /tasks`（target_platform=app）→ "
                "`GET /workstation/tasks/{task_id}/page-timeline` 等洞察接口。\n\n"
                "需 `ALIOTH_APP_QC_ENABLED=true` 且 `ALIOTH_WORKFLOW_VERSION=v2`；"
                "Runtime 走 `ALIOTH_APP_RUNTIME_PROVIDER=official`。\n\n"
                "本页 Try it out 相对当前主机的 `/api/v1`；本机平台 API 常见端口为 **8020**。"
            ),
            "version": version,
            "contact": {"name": "Alioth App QC"},
        },
        "servers": [],
        "tags": [
            {"name": "认证", "description": "JWT 登录"},
            {"name": "构建制品", "description": "上传 Android APK"},
            {"name": "质检任务", "description": "提交／查询 App 任务"},
            {"name": "工作台洞察", "description": "时间线／规则／截图／阶段投影"},
            {"name": "证据", "description": "证据文件回读"},
            {"name": "文档", "description": "OpenAPI / Swagger"},
        ],
        "paths": {
            "/api/v1/auth/login": {
                "post": {
                    "tags": ["认证"],
                    "summary": "登录获取 JWT",
                    "security": [],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["username", "password"],
                                    "properties": {
                                        "username": {
                                            "type": "string",
                                            "example": "admin",
                                        },
                                        "password": {
                                            "type": "string",
                                            "format": "password",
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "登录成功；响应含 access_token",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "access_token": {"type": "string"},
                                            "token_type": {"type": "string"},
                                            "user": {"type": "object"},
                                        },
                                    }
                                }
                            },
                        },
                        "401": {"description": "账号或密码错误"},
                    },
                }
            },
            "/api/v1/auth/me": {
                "get": {
                    "tags": ["认证"],
                    "summary": "当前登录用户",
                    "parameters": [bearer],
                    "security": [{"BearerAuth": []}],
                    "responses": {
                        "200": {"description": "用户信息"},
                        "401": {"description": "未登录"},
                    },
                }
            },
            "/api/v1/app-builds": {
                "post": {
                    "tags": ["构建制品"],
                    "summary": "上传并校验 Android APK",
                    "description": "返回 platform_build_ref（app-build:<uuid>），供提交 App 任务使用。",
                    "parameters": [bearer],
                    "security": [{"BearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "required": ["file"],
                                    "properties": {
                                        "file": {
                                            "type": "string",
                                            "format": "binary",
                                            "description": "Android APK",
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "制品已入库",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "platform_build_ref": {"type": "string"},
                                            "status": {"type": "string"},
                                            "original_filename": {"type": "string"},
                                            "sha256": {"type": "string"},
                                            "size_bytes": {"type": "integer"},
                                            "package_name": {
                                                "type": "string",
                                                "nullable": True,
                                            },
                                            "version_name": {
                                                "type": "string",
                                                "nullable": True,
                                            },
                                            "version_code": {
                                                "type": "integer",
                                                "nullable": True,
                                            },
                                        },
                                    }
                                }
                            },
                        },
                        "401": {"description": "未登录"},
                        "422": {
                            "description": "APK 校验失败",
                            "content": {
                                "application/json": {"schema": error_schema}
                            },
                        },
                    },
                }
            },
            "/api/v1/tasks": {
                "post": {
                    "tags": ["质检任务"],
                    "summary": "提交 App 质检任务",
                    "description": (
                        "正式 App 质检入口（工作台 AppQcCreate 直连此接口）。"
                        "须 ALIOTH_APP_QC_ENABLED 且 workflow v2。"
                    ),
                    "parameters": [bearer],
                    "security": [{"BearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": submit_app_task_body}
                        },
                    },
                    "responses": {
                        "202": {
                            "description": "已入队",
                            "content": {
                                "application/json": {
                                    "schema": submit_task_response
                                }
                            },
                        },
                        "401": {"description": "未登录"},
                        "404": {"description": "规则集不存在"},
                        "409": {
                            "description": "App QC 未启用或 workflow 版本不符",
                            "content": {
                                "application/json": {"schema": error_schema}
                            },
                        },
                        "422": {"description": "参数校验失败"},
                    },
                },
                "get": {
                    "tags": ["质检任务"],
                    "summary": "查询质检任务列表",
                    "parameters": [
                        bearer,
                        {
                            "name": "status",
                            "in": "query",
                            "schema": {
                                "type": "string",
                                "enum": [
                                    "queued",
                                    "running",
                                    "review",
                                    "done",
                                    "failed",
                                    "canceled",
                                ],
                            },
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {
                                "type": "integer",
                                "default": 50,
                                "minimum": 1,
                                "maximum": 100,
                            },
                        },
                    ],
                    "security": [{"BearerAuth": []}],
                    "responses": {"200": {"description": "任务列表"}},
                },
            },
            "/api/v1/tasks/{task_id}": {
                "get": {
                    "tags": ["质检任务"],
                    "summary": "查询质检任务详情",
                    "parameters": [bearer, task_id_param],
                    "security": [{"BearerAuth": []}],
                    "responses": {
                        "200": {"description": "任务详情"},
                        "404": {"description": "不存在"},
                    },
                }
            },
            "/api/v1/tasks/{task_id}/agent-run": {
                "get": {
                    "tags": ["质检任务"],
                    "summary": "最近一次 Agent Run 状态",
                    "parameters": [bearer, task_id_param],
                    "security": [{"BearerAuth": []}],
                    "responses": {"200": {"description": "Agent Run 信封"}},
                }
            },
            "/api/v1/tasks/{task_id}/rule-executions": {
                "get": {
                    "tags": ["质检任务"],
                    "summary": "规则执行结果",
                    "parameters": [bearer, task_id_param],
                    "security": [{"BearerAuth": []}],
                    "responses": {"200": {"description": "规则执行列表"}},
                }
            },
            "/api/v1/tasks/{task_id}/candidate-rules": {
                "get": {
                    "tags": ["质检任务"],
                    "summary": "候选规则（按已识别页面并集）",
                    "parameters": [
                        bearer,
                        task_id_param,
                        {
                            "name": "page_type",
                            "in": "query",
                            "schema": {"type": "string"},
                        },
                    ],
                    "security": [{"BearerAuth": []}],
                    "responses": {"200": {"description": "候选规则"}},
                }
            },
            "/api/v1/workstation/tasks/{task_id}": {
                "get": {
                    "tags": ["工作台洞察"],
                    "summary": "工作台任务详情读模型",
                    "parameters": [bearer, task_id_param],
                    "security": [{"BearerAuth": []}],
                    "responses": {
                        "200": {"description": "工作台详情"},
                        "404": {"description": "不存在"},
                    },
                }
            },
            "/api/v1/workstation/tasks/{task_id}/app-stage": {
                "get": {
                    "tags": ["工作台洞察"],
                    "summary": "App 质检阶段投影",
                    "parameters": [bearer, task_id_param],
                    "security": [{"BearerAuth": []}],
                    "responses": {
                        "200": {
                            "description": "Bootstrap／探索／Gate 等阶段",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "applicable": {"type": "boolean"},
                                            "task_id": {"type": "string"},
                                            "task_status": {"type": "string"},
                                            "bootstrap": {
                                                "type": "object",
                                                "nullable": True,
                                            },
                                            "exploration_work": {"type": "object"},
                                            "gate": {"type": "object", "nullable": True},
                                            "inspection_summary": {"type": "object"},
                                            "app_exploration": {
                                                "type": "object",
                                                "nullable": True,
                                                "properties": {
                                                    "status": {
                                                        "type": "string",
                                                        "nullable": True,
                                                        "enum": [
                                                            "exploration_completed",
                                                            "evidence_insufficient",
                                                            "close_failed",
                                                            "technical_failed",
                                                        ],
                                                    },
                                                    "surface_phase": {"type": "string"},
                                                    "coverage_summary": {
                                                        "type": "object"
                                                    },
                                                    "terminal_reason_code": {
                                                        "type": "string"
                                                    },
                                                    "outcome": {"type": "string"},
                                                    "evidence_index_id": {
                                                        "type": "string"
                                                    },
                                                    "evidence_index_hash": {
                                                        "type": "string"
                                                    },
                                                    "evidence_gap_count": {
                                                        "type": "integer"
                                                    },
                                                    "surface_session_status": {
                                                        "type": "string"
                                                    },
                                                },
                                            },
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/v1/workstation/tasks/{task_id}/evidence-index": {
                "get": {
                    "tags": ["工作台洞察"],
                    "summary": "已封存探索 Evidence Index",
                    "parameters": [bearer, task_id_param],
                    "security": [{"BearerAuth": []}],
                    "responses": {
                        "200": {"description": "时间线／谱系／制品／动作日志／缺口"},
                        "404": {"description": "尚未封存"},
                    },
                }
            },
            "/api/v1/workstation/tasks/{task_id}/page-timeline": {
                "get": {
                    "tags": ["工作台洞察"],
                    "summary": "页面识别时间线",
                    "parameters": [bearer, task_id_param],
                    "security": [{"BearerAuth": []}],
                    "responses": {"200": {"description": "时间线条目（含截图引用）"}},
                }
            },
            "/api/v1/workstation/tasks/{task_id}/rule-results": {
                "get": {
                    "tags": ["工作台洞察"],
                    "summary": "规则结果统一展示态",
                    "parameters": [
                        bearer,
                        task_id_param,
                        {
                            "name": "display_status",
                            "in": "query",
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "page_type",
                            "in": "query",
                            "schema": {"type": "string"},
                        },
                    ],
                    "security": [{"BearerAuth": []}],
                    "responses": {"200": {"description": "规则结果"}},
                }
            },
            "/api/v1/workstation/tasks/{task_id}/stats": {
                "get": {
                    "tags": ["工作台洞察"],
                    "summary": "任务统计",
                    "parameters": [bearer, task_id_param],
                    "security": [{"BearerAuth": []}],
                    "responses": {"200": {"description": "统计"}},
                }
            },
            "/api/v1/workstation/tasks/{task_id}/logs": {
                "get": {
                    "tags": ["工作台洞察"],
                    "summary": "执行日志",
                    "parameters": [bearer, task_id_param],
                    "security": [{"BearerAuth": []}],
                    "responses": {"200": {"description": "日志"}},
                }
            },
            "/api/v1/workstation/tasks/{task_id}/screenshots": {
                "get": {
                    "tags": ["工作台洞察"],
                    "summary": "截图证据列表",
                    "parameters": [bearer, task_id_param],
                    "security": [{"BearerAuth": []}],
                    "responses": {"200": {"description": "截图元数据列表"}},
                }
            },
            "/api/v1/evidence/{artifact_id}/file": {
                "get": {
                    "tags": ["证据"],
                    "summary": "回读证据文件（截图等）",
                    "parameters": [
                        bearer,
                        {
                            "name": "artifact_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "uuid"},
                        },
                    ],
                    "security": [{"BearerAuth": []}],
                    "responses": {
                        "200": {"description": "文件二进制"},
                        "404": {"description": "不存在"},
                    },
                }
            },
            "/api/v1/app-qc/openapi.json": {
                "get": {
                    "tags": ["文档"],
                    "summary": "本 OpenAPI JSON",
                    "security": [],
                    "responses": {"200": {"description": "OpenAPI 3 文档"}},
                }
            },
            "/api/v1/app-qc/api-doc": {
                "get": {
                    "tags": ["文档"],
                    "summary": "Swagger UI 对接站",
                    "security": [],
                    "responses": {"200": {"description": "HTML"}},
                }
            },
        },
        "components": {
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": "POST /api/v1/auth/login 返回的 access_token",
                }
            },
            "schemas": {},
        },
    }


def render_app_qc_swagger_html(
    *,
    openapi_url: str = "/api/v1/app-qc/openapi.json",
) -> str:
    """与 mobile-demo /api-doc 同风格的正式 App 质检 Swagger 门户。"""
    html = render_swagger_html(
        openapi_url=openapi_url,
        title="Alioth 正式 App 质检 API 对接",
    )
    replacements = (
        (
            '<a href="/api/v1/mobile-demo/demo">操作 Demo</a>',
            '<a href="/api/v1/app-qc/openapi.json" target="_blank">契约说明</a>',
        ),
        (
            '<a href="/api/v1/mobile-demo/openapi.json" target="_blank">openapi.json</a>',
            '<a href="/api/v1/app-qc/openapi.json" target="_blank">openapi.json</a>',
        ),
        (
            '<a href="http://127.0.0.1:5174/" target="_blank">玉衡工作台</a>',
            '<a href="http://127.0.0.1:5175/" target="_blank">玉衡工作台</a>',
        ),
    )
    for old, new in replacements:
        html = html.replace(old, new)
    return html
