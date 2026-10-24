"""星語 Official App Runtime OpenAPI 与 Swagger 对接站。"""

from __future__ import annotations

from typing import Any

from services.api.app.api.v1.swagger_html import render_swagger_html

_XINGYU_VERSION = "xingyu-official-v1"


def build_xingyu_official_openapi(
    *,
    version: str = _XINGYU_VERSION,
) -> dict[str, Any]:
    """组装青云对接用的 Official App OpenAPI 3.0 文档。"""
    session_body = {
        "type": "object",
        "required": ["task_id", "session_id"],
        "properties": {
            "task_id": {"type": "string", "format": "uuid"},
            "session_id": {"type": "string"},
            "surface_kind": {"type": "string", "example": "android"},
            "account_environment_ref": {"type": "string"},
            "platform_build_ref": {"type": "string"},
            "device_ref": {"type": "string", "example": "cloud_phone:default"},
            "options": {
                "type": "object",
                "properties": {
                    "backend": {
                        "type": "string",
                        "enum": ["cloud_phone", "ldplayer", "google_emulator", "redroid"],
                    }
                },
            },
        },
    }
    session_handle_props = {
        "session_handle": {
            "type": "string",
            "description": "start_session / reattach 返回的私有句柄",
        }
    }
    error_schema = {
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "error_code": {"type": "string"},
            "retryable": {"type": "boolean"},
            "message": {"type": "string"},
        },
    }
    auth_param = {
        "name": "Authorization",
        "in": "header",
        "required": True,
        "schema": {"type": "string"},
        "description": "与 ALIOTH_XINGYU_OFFICIAL_TOKEN 一致；可写 Bearer <token>",
    }
    idem_param = {
        "name": "Idempotency-Key",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
        "description": "同 key 同 payload 回缓存；同 key 异 payload → 409",
    }

    def _post(
        *,
        tag: str,
        summary: str,
        description: str,
        body: dict[str, Any],
        responses: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "tags": [tag],
            "summary": summary,
            "description": description,
            "parameters": [auth_param, idem_param],
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": body}},
            },
            "responses": {
                **responses,
                "401": {
                    "description": "未授权",
                    "content": {"application/json": {"schema": error_schema}},
                },
                "409": {
                    "description": "Idempotency 冲突",
                    "content": {"application/json": {"schema": error_schema}},
                },
            },
            "security": [{"OfficialToken": []}],
        }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Alioth 星語 Official App API",
            "description": (
                "青云 App V2 Runtime 对接契约。"
                "默认路径与 WorkflowSettings.official_app_*_path 对齐；"
                "需开启 ALIOTH_XINGYU_OFFICIAL_ENABLED 并配置 Token。"
            ),
            "version": version,
            "contact": {"name": "Alioth Xingyu Official"},
        },
        "servers": [],
        "tags": [
            {"name": "会话", "description": "start / reattach / close"},
            {"name": "应用", "description": "install / open"},
            {"name": "观测", "description": "screenshot / ui_tree"},
            {"name": "动作", "description": "execute ActionIntent"},
            {"name": "文档", "description": "OpenAPI / Swagger"},
        ],
        "paths": {
            "/v1/sessions": {
                "post": _post(
                    tag="会话",
                    summary="创建装置会话",
                    description="唤醒／就绪装置，返回 session_handle。",
                    body=session_body,
                    responses={
                        "200": {
                            "description": "会话已创建",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "request_id": {"type": "string"},
                                            "session_handle": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                )
            },
            "/v1/sessions/reattach": {
                "post": _post(
                    tag="会话",
                    summary="恢复装置会话",
                    description="按 session_id 重建装置绑定，返回新的 session_handle。",
                    body=session_body,
                    responses={
                        "200": {
                            "description": "已恢复",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "request_id": {"type": "string"},
                                            "session_handle": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                )
            },
            "/v1/sessions/close": {
                "post": _post(
                    tag="会话",
                    summary="关闭会话",
                    description="释放 session／可选 force-stop。",
                    body={
                        "type": "object",
                        "required": ["session_handle"],
                        "properties": {
                            **session_handle_props,
                            "reason": {"type": "string"},
                        },
                    },
                    responses={"200": {"description": "已关闭"}},
                )
            },
            "/v1/apps/install": {
                "post": _post(
                    tag="应用",
                    summary="安装 APK",
                    description="按 platform_build_ref + sha256 取包并安装。",
                    body={
                        "type": "object",
                        "required": [
                            "session_handle",
                            "platform_build_ref",
                            "sha256",
                        ],
                        "properties": {
                            **session_handle_props,
                            "platform_build_ref": {"type": "string"},
                            "sha256": {"type": "string"},
                            "package_name": {"type": "string"},
                            "task_id": {"type": "string"},
                            "session_id": {"type": "string"},
                        },
                    },
                    responses={
                        "200": {
                            "description": "安装结果",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "request_id": {"type": "string"},
                                            "succeeded": {"type": "boolean"},
                                            "retryable": {"type": "boolean"},
                                            "package_name": {"type": "string"},
                                            "error_code": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                )
            },
            "/v1/apps/open": {
                "post": _post(
                    tag="应用",
                    summary="打开 App",
                    description="冷／热启动并确认前景。",
                    body={
                        "type": "object",
                        "required": ["session_handle", "package_name"],
                        "properties": {
                            **session_handle_props,
                            "package_name": {"type": "string"},
                            "cold_start": {"type": "boolean", "default": True},
                            "activity": {"type": "string"},
                            "task_id": {"type": "string"},
                            "session_id": {"type": "string"},
                        },
                    },
                    responses={
                        "200": {
                            "description": "打开结果",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "request_id": {"type": "string"},
                                            "succeeded": {"type": "boolean"},
                                            "package_name": {"type": "string"},
                                            "foreground_verified": {"type": "boolean"},
                                            "activity": {"type": "string"},
                                            "error_code": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                )
            },
            "/v1/apps/capture": {
                "post": _post(
                    tag="观测",
                    summary="截取 PNG",
                    description=(
                        "成功时响应体为 image/png；"
                        "头字段含 x-alioth-width/height/orientation/sha256。"
                    ),
                    body={
                        "type": "object",
                        "required": ["session_handle"],
                        "properties": {
                            **session_handle_props,
                            "task_id": {"type": "string"},
                            "session_id": {"type": "string"},
                            "surface_ref": {"type": "string"},
                            "capture_profile": {"type": "object"},
                        },
                    },
                    responses={
                        "200": {
                            "description": "PNG 截图",
                            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
                            "headers": {
                                "x-alioth-width": {
                                    "schema": {"type": "integer"},
                                    "description": "像素宽",
                                },
                                "x-alioth-height": {
                                    "schema": {"type": "integer"},
                                    "description": "像素高",
                                },
                                "x-alioth-orientation": {
                                    "schema": {
                                        "type": "string",
                                        "enum": ["portrait", "landscape", "unknown"],
                                    }
                                },
                                "x-alioth-sha256": {"schema": {"type": "string"}},
                                "x-request-id": {"schema": {"type": "string"}},
                            },
                        }
                    },
                )
            },
            "/v1/apps/capture-ui-tree": {
                "post": _post(
                    tag="观测",
                    summary="拉取 UI Hierarchy",
                    description="uiautomator dump，返回 application/xml。",
                    body={
                        "type": "object",
                        "required": ["session_handle"],
                        "properties": {
                            **session_handle_props,
                            "task_id": {"type": "string"},
                            "session_id": {"type": "string"},
                            "capture_profile": {"type": "object"},
                        },
                    },
                    responses={
                        "200": {
                            "description": "hierarchy XML",
                            "content": {
                                "application/xml": {
                                    "schema": {"type": "string", "format": "binary"}
                                }
                            },
                        }
                    },
                )
            },
            "/v1/apps/capture-aux": {
                "post": _post(
                    tag="观测",
                    summary="拉取辅助证据",
                    description="device_log / network_trace / fault_artifact 有界 JSON。",
                    body={
                        "type": "object",
                        "required": ["session_handle", "artifact_type"],
                        "properties": {
                            **session_handle_props,
                            "task_id": {"type": "string"},
                            "session_id": {"type": "string"},
                            "artifact_type": {
                                "type": "string",
                                "enum": [
                                    "device_log",
                                    "network_trace",
                                    "fault_artifact",
                                ],
                            },
                            "package_name": {"type": "string"},
                        },
                    },
                    responses={
                        "200": {
                            "description": "辅助证据 JSON",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": [
                                            "request_id",
                                            "artifact_type",
                                            "payload",
                                        ],
                                        "properties": {
                                            "request_id": {"type": "string"},
                                            "artifact_type": {"type": "string"},
                                            "content_type": {"type": "string"},
                                            "payload": {"type": "object"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                )
            },
            "/v1/apps/execute": {
                "post": _post(
                    tag="动作",
                    summary="执行 ActionIntent",
                    description="解析 click/scroll/back/observe 等并回写 action_trace。",
                    body={
                        "type": "object",
                        "required": ["session_handle", "action_execution_id", "intent"],
                        "properties": {
                            **session_handle_props,
                            "action_execution_id": {"type": "string"},
                            "surface_ref": {"type": "string"},
                            "intent": {
                                "type": "object",
                                "required": ["action_type"],
                                "properties": {
                                    "action_type": {
                                        "type": "string",
                                        "enum": [
                                            "click",
                                            "tap",
                                            "scroll",
                                            "back",
                                            "observe",
                                            "timed_capture",
                                        ],
                                    },
                                    "semantic_target": {"type": "string"},
                                    "parameters": {"type": "object"},
                                },
                            },
                        },
                    },
                    responses={
                        "200": {
                            "description": "动作结果",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "request_id": {"type": "string"},
                                            "action_execution_id": {"type": "string"},
                                            "succeeded": {"type": "boolean"},
                                            "retryable": {"type": "boolean"},
                                            "action_trace": {
                                                "type": "array",
                                                "items": {"type": "object"},
                                            },
                                            "error_code": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                )
            },
            "/v1/openapi.json": {
                "get": {
                    "tags": ["文档"],
                    "summary": "OpenAPI 3 规格",
                    "responses": {"200": {"description": "OpenAPI JSON"}},
                }
            },
            "/v1/api-doc": {
                "get": {
                    "tags": ["文档"],
                    "summary": "Swagger UI 对接站",
                    "responses": {"200": {"description": "HTML"}},
                }
            },
        },
        "components": {
            "securitySchemes": {
                "OfficialToken": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "Authorization",
                    "description": "ALIOTH_XINGYU_OFFICIAL_TOKEN；可写 Bearer <token>",
                }
            }
        },
    }


def render_xingyu_swagger_html(*, openapi_url: str = "/v1/openapi.json") -> str:
    """与 mobile-demo /api-doc 同风格的 Swagger 门户。"""
    # 复用同一套 HTML 外壳；导航链到 Official 规格
    html = render_swagger_html(
        openapi_url=openapi_url,
        title="Alioth 星語 Official App API",
    )
    return (
        html.replace(
            '<a href="/api/v1/mobile-demo/demo">操作 Demo</a>',
            '<a href="/v1/openapi.json" target="_blank">契约说明</a>',
        )
        .replace(
            '<a href="/api/v1/mobile-demo/openapi.json" target="_blank">openapi.json</a>',
            '<a href="/v1/openapi.json" target="_blank">openapi.json</a>',
        )
    )
