"""MaiBot 模型路由插件。

按 QQ 群号或私聊 QQ 号为指定会话配置模型路由。每一条路由规则同时包含
Planner 与 Replyer 两组模型字段，插件只负责“选择模型名称”，不会保存或
覆盖 API Key、base_url、temperature 等模型参数；实际模型参数仍来自
MaiBot 的 model_config 配置。
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase

logger = logging.getLogger("model_router_plugin")

PLUGIN_VERSION = "1.0.0"
HOOK_TYPE = "HOOK_HANDLER"
HOOK_MODE = "blocking"
HOOK_ORDER = "early"
HOOK_TIMEOUT_MS = 5000


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件开关"
    __ui_icon__ = "power"
    __ui_order__ = 0

    enabled: bool = Field(
        default=False,
        description=(
            "是否启用模型路由插件。关闭后插件不会接管任何模型选择，"
            "所有会话都继续使用 MaiBot 的 model_config 默认模型配置。"
        ),
    )
    config_version: str = Field(default=PLUGIN_VERSION, description="插件配置版本号。请不要手动修改。")


class LoggingConfig(PluginConfigBase):
    """日志配置。"""

    __ui_label__ = "日志"
    __ui_icon__ = "terminal"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="是否输出模型路由命中、未命中、配置格式错误等中文日志。")


class RouteItemConfig(PluginConfigBase):
    """单条路由配置。"""

    enabled: bool = Field(default=True, description="是否启用这一条路由。")
    name: str = Field(default="示例路由", description="路由名称，仅用于日志显示。")
    target_type: str = Field(default="group", description="会话类型：group 表示群聊，private 表示私聊。")
    target_id: str = Field(default="", description="群聊填写QQ群号；私聊填写对方QQ号。")
    planner_models: List[str] = Field(default_factory=list, description="Planner 使用的模型名称列表。")
    replyer_models: List[str] = Field(default_factory=list, description="Replyer 使用的模型名称列表。")


class ModelRouterPluginConfig(PluginConfigBase):
    """模型路由插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    routes: List[RouteItemConfig] = Field(default_factory=list)


@dataclass(frozen=True)
class ParsedRoute:
    """解析后的单条路由规则。"""

    enabled: bool
    name: str
    target_type: str
    target_id: str
    planner_models: List[str]
    replyer_models: List[str]

    def models_for_stage(self, stage: str) -> List[str]:
        """按阶段返回模型列表。"""

        return self.planner_models if stage == "planner" else self.replyer_models


class ModelRouterPlugin(MaiBotPlugin):
    """按 QQ 群号或私聊 QQ 号路由 Planner / Replyer 模型。"""

    config_model = ModelRouterPluginConfig

    def __init__(self) -> None:
        """初始化运行时缓存。"""

        super().__init__()
        self._cached_account_ids: List[str] = []

    async def on_load(self) -> None:
        """插件加载时输出提示。"""

        self._refresh_possible_account_ids(allow_import=True)
        self._install_llm_service_patch()
        self._log_info(
            "模型路由插件已加载。"
            f"已缓存机器人账号：{', '.join(self._cached_account_ids) if self._cached_account_ids else '未读取到'}。"
            "已启用 LLMService 调用层路由补丁。"
        )

    async def on_unload(self) -> None:
        """插件卸载时输出提示。"""

        self._uninstall_llm_service_patch()
        self._log_info("模型路由插件已卸载。")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """处理配置热重载事件。"""

        del scope
        del config_data
        self._refresh_possible_account_ids(allow_import=True)
        self._log_info(f"模型路由插件配置已热重载，版本：{version or '未知'}。")

    def get_default_config(self) -> Dict[str, Any]:
        """返回默认配置，生成用户期望的 [[routes]] 数组表结构。"""

        return {
            "plugin": {"enabled": False, "config_version": PLUGIN_VERSION},
            "logging": {"enabled": True},
            "routes": [
                {
                    "enabled": True,
                    "name": "示例群聊路由",
                    "target_type": "group",
                    "target_id": "123456789",
                    "planner_models": ["模型2", "模型3"],
                    "replyer_models": ["模型2", "模型3"],
                },
                {
                    "enabled": True,
                    "name": "示例私聊路由",
                    "target_type": "private",
                    "target_id": "987654321",
                    "planner_models": ["模型C"],
                    "replyer_models": ["模型D", "模型E"],
                },
            ],
        }

    def normalize_plugin_config(self, config_data: Optional[Mapping[str, Any]]) -> Tuple[Dict[str, Any], bool]:
        """归一化配置，保留 [[routes]] 中的用户填写内容。"""

        raw_config = dict(config_data or {})
        normalized: Dict[str, Any] = {
            "plugin": {"enabled": False, "config_version": PLUGIN_VERSION},
            "logging": {"enabled": True},
            "routes": [],
        }

        plugin_section = raw_config.get("plugin")
        if isinstance(plugin_section, Mapping):
            normalized["plugin"]["enabled"] = self._normalize_bool(plugin_section.get("enabled", False))
        normalized["plugin"]["config_version"] = PLUGIN_VERSION

        logging_section = raw_config.get("logging")
        if isinstance(logging_section, Mapping):
            normalized["logging"]["enabled"] = self._normalize_bool(logging_section.get("enabled", True))

        raw_routes = raw_config.get("routes")
        if isinstance(raw_routes, list):
            normalized["routes"] = self._normalize_routes_list(raw_routes)
        elif isinstance(raw_routes, Mapping):
            # 兼容上一版字符串列表 routes.group_routes/private_routes。
            normalized["routes"] = self._migrate_string_routes(raw_routes)

        # 兼容更早一版 planner/replyer 分开配置，合并为 [[routes]]。
        split_routes = self._migrate_split_stage_routes(raw_config)
        existing_keys = {
            (str(route.get("target_type", "")), str(route.get("target_id", ""))) for route in normalized["routes"]
        }
        for route in split_routes:
            key = (str(route.get("target_type", "")), str(route.get("target_id", "")))
            if key not in existing_keys:
                normalized["routes"].append(route)
                existing_keys.add(key)

        changed = normalized != raw_config
        return normalized, changed

    def get_webui_config_schema(
        self,
        *,
        plugin_id: str = "",
        plugin_name: str = "",
        plugin_version: str = "",
        plugin_description: str = "",
        plugin_author: str = "",
    ) -> Dict[str, Any]:
        """返回 WebUI 配置 Schema：一条路由里直接编辑 planner/replyer 模型名。"""

        return {
            "plugin_id": plugin_id,
            "plugin_info": {
                "name": plugin_name or "模型路由插件",
                "version": plugin_version or PLUGIN_VERSION,
                "description": plugin_description or "按 QQ 群号或私聊 QQ 号同时配置 Planner / Replyer 模型。",
                "author": plugin_author or "",
            },
            "sections": {
                "plugin": {
                    "name": "plugin",
                    "title": "插件开关",
                    "description": "控制模型路由插件是否启用。",
                    "icon": "power",
                    "collapsed": False,
                    "order": 0,
                    "fields": {
                        "enabled": self._schema_field(
                            "enabled",
                            "boolean",
                            "启用模型路由",
                            False,
                            "开启后，命中路由的群聊或私聊会使用指定模型；关闭后全部走默认 model_config。",
                            ui_type="switch",
                            order=0,
                        ),
                        "config_version": self._schema_field(
                            "config_version",
                            "string",
                            "配置版本",
                            PLUGIN_VERSION,
                            "插件配置版本号，请不要手动修改。",
                            disabled=True,
                            order=1,
                        ),
                    },
                },
                "logging": {
                    "name": "logging",
                    "title": "日志",
                    "description": "控制是否输出模型路由命中情况。",
                    "icon": "terminal",
                    "collapsed": False,
                    "order": 1,
                    "fields": {
                        "enabled": self._schema_field(
                            "enabled",
                            "boolean",
                            "输出中文日志",
                            True,
                            "开启后会记录路由命中、未命中、规则格式错误等信息。",
                            ui_type="switch",
                            order=0,
                        )
                    },
                },
                "route_config": {
                    # WebUI 的插件配置页会把 section.name 当作配置路径。
                    # "." 会被拆成空路径，因此该 section 内的 routes 字段会绑定到根级 config["routes"]，
                    # 既能在 WebUI 以“一个号码一组”的对象列表显示，也能保存为 TOML 的 [[routes]]。
                    "name": ".",
                    "title": "会话模型路由",
                    "description": "每一组就是一个 QQ/QQ群号路由，组内同时填写 Planner 模型和 Replyer 模型。",
                    "icon": "route",
                    "collapsed": False,
                    "order": 2,
                    "fields": {
                        "routes": self._schema_routes_list_field(),
                    },
                },
            },
            "layout": {"type": "auto", "tabs": []},
        }

    def get_components(self) -> List[Dict[str, Any]]:
        """手动声明 HookHandler 组件。"""

        return [
            {
                "name": "planner_model_router",
                "type": HOOK_TYPE,
                "metadata": {
                    "handler_name": "handle_planner_before_request",
                    "hook": "maisaka.planner.before_request",
                    "mode": HOOK_MODE,
                    "order": HOOK_ORDER,
                    "timeout_ms": HOOK_TIMEOUT_MS,
                    "error_policy": "skip",
                    "description": "Planner 模型路由：根据当前会话匹配 QQ 群号或私聊 QQ 号。",
                },
            },
            {
                "name": "replyer_model_router",
                "type": HOOK_TYPE,
                "metadata": {
                    "handler_name": "handle_replyer_before_request",
                    "hook": "maisaka.replyer.before_request",
                    "mode": HOOK_MODE,
                    "order": HOOK_ORDER,
                    "timeout_ms": HOOK_TIMEOUT_MS,
                    "error_policy": "skip",
                    "description": "Replyer 模型路由：根据当前会话匹配 QQ 群号或私聊 QQ 号。",
                },
            },
        ]

    async def handle_planner_before_request(self, **kwargs: Any) -> Dict[str, Any]:
        """处理 Planner 请求前 Hook。"""

        return self._handle_model_route(stage="planner", kwargs=kwargs)

    async def handle_replyer_before_request(self, **kwargs: Any) -> Dict[str, Any]:
        """处理 Replyer 请求前 Hook。"""

        return self._handle_model_route(stage="replyer", kwargs=kwargs)

    def _install_llm_service_patch(self) -> None:
        """安装 LLMService 调用层补丁。

        当前 MaiBot 的 Planner before_request Hook 虽然传入 session_id，
        但不会消费 Hook 返回的 model_name；因此仅靠 Hook 无法改变 Planner
        实际使用的模型。这里在插件加载时无侵入补丁 LLMServiceClient，
        在真正发起模型请求前根据 client.session_id 改写 options.model_name。
        """

        try:
            llm_service = importlib.import_module("src.services.llm_service")
            data_models = importlib.import_module("src.common.data_models.llm_service_data_models")
            chat_loop_service = importlib.import_module("src.maisaka.chat_loop_service")
            utils_model = importlib.import_module("src.llm_models.utils_model")
        except Exception as exc:
            self._log_info(f"安装 LLMService 调用层路由补丁失败，将仅使用 Hook 路由：{exc}")
            return

        client_cls = getattr(llm_service, "LLMServiceClient", None)
        options_cls = getattr(data_models, "LLMGenerationOptions", None)
        chat_loop_cls = getattr(chat_loop_service, "MaisakaChatLoopService", None)
        orchestrator_cls = getattr(utils_model, "LLMOrchestrator", None)
        if client_cls is None or options_cls is None:
            self._log_info("安装 LLMService 调用层路由补丁失败：找不到 LLMServiceClient 或 LLMGenerationOptions。")
            return

        if not hasattr(client_cls, "_model_router_original_generate_response"):
            client_cls._model_router_original_generate_response = client_cls.generate_response
        if not hasattr(client_cls, "_model_router_original_generate_response_with_messages"):
            client_cls._model_router_original_generate_response_with_messages = client_cls.generate_response_with_messages
        if not hasattr(client_cls, "_model_router_generation_options_cls"):
            client_cls._model_router_generation_options_cls = options_cls

        if orchestrator_cls is not None and not getattr(orchestrator_cls, "_model_router_patch_installed", False):
            original_execute_request = orchestrator_cls._execute_request

            async def routed_execute_request(orchestrator_self: Any, *args: Any, **kwargs: Any) -> Any:
                active_plugin = getattr(client_cls, "_model_router_active_plugin", None)
                forced_model_name = str(getattr(orchestrator_self, "_model_router_forced_model_name", "") or "").strip()
                if active_plugin is not None and forced_model_name and not str(kwargs.get("model_name") or "").strip():
                    kwargs["model_name"] = forced_model_name
                    active_plugin._log_info(
                        f"LLMOrchestrator 最终请求层应用模型路由："
                        f"request_type={getattr(orchestrator_self, 'request_type', '') or '未知'}，"
                        f"model={forced_model_name}"
                    )
                return await original_execute_request(orchestrator_self, *args, **kwargs)

            orchestrator_cls._model_router_original_execute_request = original_execute_request
            orchestrator_cls._execute_request = routed_execute_request
            orchestrator_cls._model_router_patch_installed = True

        if chat_loop_cls is not None and not getattr(chat_loop_cls, "_model_router_patch_installed", False):
            original_get_llm_chat_client = chat_loop_cls._get_llm_chat_client

            def routed_get_llm_chat_client(service_self: Any, request_kind: str) -> Any:
                llm_client = original_get_llm_chat_client(service_self, request_kind)
                active_plugin = getattr(client_cls, "_model_router_active_plugin", None)
                if active_plugin is not None:
                    active_plugin._apply_chat_loop_route_to_llm_client(
                        llm_client,
                        session_id=str(getattr(service_self, "_session_id", "") or "").strip(),
                        request_kind=str(request_kind or "").strip(),
                    )
                return llm_client

            chat_loop_cls._model_router_original_get_llm_chat_client = original_get_llm_chat_client
            chat_loop_cls._get_llm_chat_client = routed_get_llm_chat_client
            chat_loop_cls._model_router_patch_installed = True

        if not getattr(client_cls, "_model_router_patch_installed", False):
            original_generate_response = client_cls._model_router_original_generate_response
            original_generate_response_with_messages = client_cls._model_router_original_generate_response_with_messages

            async def routed_generate_response(client_self: Any, prompt: str, options: Any = None) -> Any:
                active_plugin = getattr(client_cls, "_model_router_active_plugin", None)
                if active_plugin is None:
                    return await original_generate_response(client_self, prompt, options)
                routed_options = active_plugin._route_llm_generation_options(client_self, options)
                return await original_generate_response(client_self, prompt, routed_options)

            async def routed_generate_response_with_messages(
                client_self: Any,
                message_factory: Any,
                options: Any = None,
            ) -> Any:
                active_plugin = getattr(client_cls, "_model_router_active_plugin", None)
                if active_plugin is None:
                    return await original_generate_response_with_messages(client_self, message_factory, options)
                routed_options = active_plugin._route_llm_generation_options(client_self, options)
                return await original_generate_response_with_messages(client_self, message_factory, routed_options)

            client_cls.generate_response = routed_generate_response
            client_cls.generate_response_with_messages = routed_generate_response_with_messages
            client_cls._model_router_patch_installed = True

        client_cls._model_router_active_plugin = self

    def _uninstall_llm_service_patch(self) -> None:
        """卸载或停用 LLMService 调用层补丁。"""

        try:
            llm_service = sys.modules.get("src.services.llm_service")
            client_cls = getattr(llm_service, "LLMServiceClient", None) if llm_service is not None else None
            if client_cls is None:
                return

            if getattr(client_cls, "_model_router_active_plugin", None) is self:
                client_cls._model_router_active_plugin = None

            original_generate_response = getattr(client_cls, "_model_router_original_generate_response", None)
            original_generate_response_with_messages = getattr(
                client_cls,
                "_model_router_original_generate_response_with_messages",
                None,
            )
            if original_generate_response is not None and original_generate_response_with_messages is not None:
                client_cls.generate_response = original_generate_response
                client_cls.generate_response_with_messages = original_generate_response_with_messages
                client_cls._model_router_patch_installed = False

            chat_loop_service = sys.modules.get("src.maisaka.chat_loop_service")
            chat_loop_cls = (
                getattr(chat_loop_service, "MaisakaChatLoopService", None) if chat_loop_service is not None else None
            )
            original_get_llm_chat_client = (
                getattr(chat_loop_cls, "_model_router_original_get_llm_chat_client", None)
                if chat_loop_cls is not None
                else None
            )
            if chat_loop_cls is not None and original_get_llm_chat_client is not None:
                chat_loop_cls._get_llm_chat_client = original_get_llm_chat_client
                chat_loop_cls._model_router_patch_installed = False

            utils_model = sys.modules.get("src.llm_models.utils_model")
            orchestrator_cls = getattr(utils_model, "LLMOrchestrator", None) if utils_model is not None else None
            original_execute_request = (
                getattr(orchestrator_cls, "_model_router_original_execute_request", None)
                if orchestrator_cls is not None
                else None
            )
            if orchestrator_cls is not None and original_execute_request is not None:
                orchestrator_cls._execute_request = original_execute_request
                orchestrator_cls._model_router_patch_installed = False
        except Exception:
            return

    def _apply_chat_loop_route_to_llm_client(self, llm_client: Any, *, session_id: str, request_kind: str) -> None:
        """把 Planner 路由直接写入当前 ChatLoop 对应的 LLM 客户端/Orchestrator。

        这是 Planner 的关键补强点：ChatLoop 最清楚当前 session_id 与 request_kind，
        这里把命中的模型写到 orchestrator 上，最终由 _execute_request 补丁兜底消费。
        """

        if not self._plugin_enabled or not session_id:
            return

        stage = self._resolve_stage_from_request_kind(request_kind)
        if stage not in {"planner", "replyer"}:
            return

        matched = self._find_matched_route(session_id=session_id)
        if matched is None:
            return

        route_models = matched.models_for_stage(stage)
        if not route_models:
            return

        routed_model_name = route_models[0]
        orchestrator = getattr(llm_client, "_orchestrator", None)
        if orchestrator is not None:
            setattr(orchestrator, "_model_router_forced_model_name", routed_model_name)
            setattr(orchestrator, "_model_router_forced_session_id", session_id)
            setattr(orchestrator, "_model_router_forced_stage", stage)

        setattr(llm_client, "_model_router_forced_model_name", routed_model_name)

        self._log_info(
            f"ChatLoop 客户端层命中{self._chat_type_label(matched.target_type)} {matched.target_id} "
            f"的路由“{matched.name}”：{self._stage_label(stage)} 将强制使用 {routed_model_name}；"
            f"session_id={session_id}，request_kind={request_kind or 'planner'}"
        )

    def _route_llm_generation_options(self, llm_client: Any, options: Any = None) -> Any:
        """在 LLMServiceClient 真正请求模型前按会话改写 model_name。"""

        if not self._plugin_enabled:
            return options

        stage = self._resolve_stage_from_llm_client(llm_client)
        if stage not in {"planner", "replyer"}:
            return options

        session_id = str(getattr(llm_client, "session_id", "") or "").strip()
        if not session_id:
            return options

        matched = self._find_matched_route(session_id=session_id)
        if matched is None:
            return options

        route_models = matched.models_for_stage(stage)
        if not route_models:
            return options

        routed_model_name = route_models[0]
        orchestrator = getattr(llm_client, "_orchestrator", None)
        if orchestrator is not None:
            setattr(orchestrator, "_model_router_forced_model_name", routed_model_name)
            setattr(orchestrator, "_model_router_forced_session_id", session_id)
            setattr(orchestrator, "_model_router_forced_stage", stage)
        routed_options = self._copy_generation_options(options)
        if routed_options is None:
            return options

        current_model_name = str(getattr(routed_options, "model_name", "") or "").strip()
        routed_options.model_name = routed_model_name

        self._log_info(
            f"LLMService 调用层命中{self._chat_type_label(matched.target_type)} {matched.target_id} "
            f"的路由“{matched.name}”：{self._stage_label(stage)} "
            f"{current_model_name or '默认模型'} -> {routed_model_name}；session_id={session_id}"
        )
        return routed_options

    @staticmethod
    def _resolve_stage_from_llm_client(llm_client: Any) -> str:
        """根据 LLMServiceClient 的 task_name/request_type 判断路由阶段。"""

        task_name = str(getattr(llm_client, "task_name", "") or "").strip().lower()
        request_type = str(getattr(llm_client, "request_type", "") or "").strip().lower()

        if "replyer" in task_name or "replyer" in request_type:
            return "replyer"

        if (
            task_name == "planner"
            or request_type in {
                "maisaka_planner",
                "maisaka_timing_gate",
                "maisaka_sub_agent",
            }
            or request_type.startswith("maisaka_")
        ):
            return "planner"

        return ""

    @staticmethod
    def _resolve_stage_from_request_kind(request_kind: str) -> str:
        """根据 Maisaka ChatLoop 的 request_kind 判断路由阶段。"""

        normalized = str(request_kind or "").strip().lower()
        if normalized in {"", "planner", "timing_gate", "sub_agent"}:
            return "planner"
        if "replyer" in normalized:
            return "replyer"
        return ""

    @staticmethod
    def _copy_generation_options(options: Any = None) -> Any:
        """复制 LLMGenerationOptions，避免直接修改调用方对象。"""

        options_cls = None
        try:
            llm_service = sys.modules.get("src.services.llm_service")
            client_cls = getattr(llm_service, "LLMServiceClient", None) if llm_service is not None else None
            options_cls = getattr(client_cls, "_model_router_generation_options_cls", None)
        except Exception:
            options_cls = None

        if options_cls is None:
            try:
                data_models = importlib.import_module("src.common.data_models.llm_service_data_models")
                options_cls = getattr(data_models, "LLMGenerationOptions", None)
            except Exception:
                options_cls = None

        if options_cls is None:
            return options

        if options is None:
            return options_cls()

        return options_cls(
            temperature=getattr(options, "temperature", None),
            max_tokens=getattr(options, "max_tokens", None),
            model_name=getattr(options, "model_name", None),
            tool_options=getattr(options, "tool_options", None),
            response_format=getattr(options, "response_format", None),
            interrupt_flag=getattr(options, "interrupt_flag", None),
            raise_when_empty=getattr(options, "raise_when_empty", True),
        )

    @staticmethod
    def _strip_runtime_only_kwargs(kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        """移除 HookDispatcher 注入的运行时字段，避免污染 Hook 原始参数。"""

        modified = dict(kwargs)
        modified.pop("hook_name", None)
        return modified

    def _handle_model_route(self, *, stage: str, kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        """按阶段执行模型路由并返回 Hook 修改结果。"""

        modified_kwargs = self._strip_runtime_only_kwargs(kwargs)
        if not self._plugin_enabled:
            return {"action": "continue", "modified_kwargs": modified_kwargs}

        session_id = self._resolve_hook_session_id(kwargs)
        if not session_id:
            self._log_info(
                f"{self._stage_label(stage)} 未提供可识别的 session_id，使用默认 model_config 配置。"
                f"Hook 参数字段：{', '.join(sorted(str(key) for key in kwargs.keys()))}"
            )
            return {"action": "continue", "modified_kwargs": modified_kwargs}

        matched = self._find_matched_route(session_id=session_id)
        if matched is None:
            self._log_info(
                f"当前会话未命中模型路由，{self._stage_label(stage)} 使用默认 model_config 配置。"
                f"session_id={session_id}"
            )
            return {"action": "continue", "modified_kwargs": modified_kwargs}

        route_models = matched.models_for_stage(stage)
        if not route_models:
            self._log_info(
                f"命中路由“{matched.name}”，但未配置 {self._stage_label(stage)} 模型，"
                "继续使用默认 model_config。"
            )
            return {"action": "continue", "modified_kwargs": modified_kwargs}

        current_task_name = str(kwargs.get("task_name") or stage).strip() or stage
        default_model_name = str(kwargs.get("model_name") or "").strip()

        modified_kwargs["model_name"] = route_models[0]
        modified_kwargs["requested_model_name"] = route_models[0]
        modified_kwargs["model_router_enabled"] = True
        modified_kwargs["model_router_route_name"] = matched.name
        modified_kwargs["model_router_target_type"] = matched.target_type
        modified_kwargs["model_router_target_id"] = matched.target_id
        modified_kwargs["model_router_stage"] = stage
        modified_kwargs["model_router_models"] = list(route_models)
        modified_kwargs["model_list"] = list(route_models)
        modified_kwargs["fallback_model_name"] = default_model_name
        modified_kwargs["fallback_task_name"] = current_task_name

        self._log_info(
            f"命中{self._chat_type_label(matched.target_type)} {matched.target_id} 的路由“{matched.name}”："
            f"{self._stage_label(stage)} 优先模型顺序为 {self._format_model_chain(route_models)}；"
            "模型 API 与参数仍使用 model_config 中对应模型的配置。"
        )

        if stage == "planner":
            self._log_info(
                "兼容提示：当前版本 MaiBot 的 maisaka.planner.before_request Hook "
                "如尚未消费 model_name/model_list，Planner 路由字段会被安全忽略。"
            )
        elif len(route_models) > 1:
            self._log_info(
                "兼容提示：当前版本 MaiBot 显式 model_name 调用通常只尝试第一个模型；"
                "本插件已同时返回 model_list 兼容字段，供支持多模型 Hook 的版本使用。"
            )

        return {"action": "continue", "modified_kwargs": modified_kwargs}

    @staticmethod
    def _resolve_hook_session_id(kwargs: Mapping[str, Any]) -> str:
        """从不同版本 Hook 参数中解析真实 session_id。"""

        direct_keys = (
            "session_id",
            "stream_id",
            "chat_id",
            "chat_stream_id",
            "conversation_id",
            "conversation_key",
        )
        for key in direct_keys:
            value = str(kwargs.get(key) or "").strip()
            if value:
                return value

        object_keys = ("chat_stream", "session", "chat_session", "message", "msg")
        attr_keys = ("session_id", "stream_id", "chat_id")
        for object_key in object_keys:
            obj = kwargs.get(object_key)
            if obj is None:
                continue
            for attr_key in attr_keys:
                value = str(getattr(obj, attr_key, "") or "").strip()
                if value:
                    return value
            if isinstance(obj, Mapping):
                for attr_key in attr_keys:
                    value = str(obj.get(attr_key) or "").strip()
                    if value:
                        return value

        runtime = kwargs.get("runtime")
        if runtime is not None:
            value = str(getattr(runtime, "session_id", "") or "").strip()
            if value:
                return value

        return ""

    @property
    def _plugin_enabled(self) -> bool:
        """返回插件配置开关是否开启。"""

        try:
            return bool(self.config.plugin.enabled)
        except Exception:
            return False

    @property
    def _logging_enabled(self) -> bool:
        """返回日志开关是否开启。"""

        try:
            return bool(self.config.logging.enabled)
        except Exception:
            return True

    def _log_info(self, message: str) -> None:
        """输出中文信息日志。"""

        if self._logging_enabled:
            logger.info("[模型路由] %s", message)

    def _find_matched_route(self, *, session_id: str) -> Optional[ParsedRoute]:
        """查找第一条匹配会话的统一路由。"""

        for route in self._iter_parsed_routes():
            if not route.enabled:
                continue
            if self._route_matches_session(
                session_id=session_id,
                target_type=route.target_type,
                target_id=route.target_id,
            ):
                return route
        return None

    def _iter_parsed_routes(self) -> Iterable[ParsedRoute]:
        """迭代当前配置中的路由。"""

        raw_routes = getattr(self.config, "routes", None)
        if raw_routes is None:
            raw_routes = getattr(getattr(self.config, "route_config", object()), "routes", [])
        if raw_routes is None:
            raw_routes = []
        if not isinstance(raw_routes, list):
            return

        for raw_route in raw_routes:
            parsed = self._parse_route(raw_route)
            if parsed is not None:
                yield parsed

    def _parse_route(self, raw_route: Any) -> Optional[ParsedRoute]:
        """解析单条路由配置。"""

        if raw_route is None:
            self._log_info("跳过空路由配置。")
            return None

        target_type = self._normalize_target_type(self._route_value(raw_route, "target_type", ""))
        target_id = str(self._route_value(raw_route, "target_id", "") or "").strip()
        if target_type not in {"group", "private"} or not target_id:
            self._log_info(f"跳过目标不完整的路由配置：{raw_route}")
            return None

        return ParsedRoute(
            enabled=self._normalize_bool(self._route_value(raw_route, "enabled", True)),
            name=str(self._route_value(raw_route, "name", "") or "").strip()
            or f"{self._chat_type_label(target_type)} {target_id}",
            target_type=target_type,
            target_id=target_id,
            planner_models=self._normalize_model_names(self._route_value(raw_route, "planner_models", [])),
            replyer_models=self._normalize_model_names(self._route_value(raw_route, "replyer_models", [])),
        )

    @staticmethod
    def _route_value(raw_route: Any, key: str, default: Any) -> Any:
        """从 dict / tomlkit table / Pydantic 配置对象中读取路由字段。"""

        if isinstance(raw_route, Mapping):
            return raw_route.get(key, default)
        return getattr(raw_route, key, default)

    def _route_matches_session(self, *, session_id: str, target_type: str, target_id: str) -> bool:
        """判断路由目标是否匹配当前 session_id。"""

        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return False

        if normalized_session_id == target_id:
            return True

        candidate_session_ids = self._build_candidate_session_ids(target_type=target_type, target_id=target_id)
        if normalized_session_id in candidate_session_ids:
            return True

        compact_session = normalized_session_id.replace("-", "_").replace(":", "_")
        compact_target = target_id.replace("-", "_").replace(":", "_")
        if compact_target in compact_session:
            return True

        chat_session = self._get_chat_session(normalized_session_id)
        if chat_session is None:
            return False

        if target_type == "group":
            candidate_values = (
                getattr(chat_session, "group_id", ""),
                getattr(chat_session, "group", ""),
                getattr(chat_session, "chat_id", ""),
            )
        else:
            candidate_values = (
                getattr(chat_session, "user_id", ""),
                getattr(chat_session, "person_id", ""),
                getattr(chat_session, "chat_id", ""),
            )

        return any(str(value or "").strip() == target_id for value in candidate_values)

    def _build_candidate_session_ids(self, *, target_type: str, target_id: str) -> set[str]:
        """根据 QQ 号/群号推导可能的 MaiBot session_id。"""

        candidates: set[str] = {target_id}
        platforms = ["qq", "webui"]
        account_ids = ["", *self._get_possible_account_ids()]
        scopes = [""]

        for platform in platforms:
            for account_id in account_ids:
                for scope in scopes:
                    components: List[str] = [platform]
                    if account_id:
                        components.append(f"account:{account_id}")
                    if scope:
                        components.append(f"scope:{scope}")
                    if target_type == "group":
                        components.append(target_id)
                    else:
                        components.extend([target_id, "private"])
                    candidates.add(self._md5_session_components(components))

        if target_type == "group":
            candidates.update({f"group:{target_id}", f"qq:group:{target_id}", f"qq_group_{target_id}"})
        else:
            candidates.update({f"private:{target_id}", f"qq:private:{target_id}", f"qq_private_{target_id}"})

        return {candidate for candidate in candidates if candidate}

    @staticmethod
    def _get_chat_session(session_id: str) -> Any:
        """从已加载的 chat_manager 中读取真实会话，辅助匹配 QQ号/群号。"""

        for module_name in (
            "src.chat.message_receive.chat_manager",
            "chat.message_receive.chat_manager",
        ):
            module = sys.modules.get(module_name)
            if module is None:
                continue
            chat_manager = getattr(module, "chat_manager", None)
            if chat_manager is None:
                chat_manager = getattr(module, "_chat_manager", None)
            if chat_manager is None:
                continue
            getter = getattr(chat_manager, "get_session_by_session_id", None)
            if not callable(getter):
                continue
            try:
                return getter(session_id)
            except Exception:
                return None
        return None

    def _normalize_routes_list(self, raw_routes: List[Any]) -> List[Dict[str, Any]]:
        """规范化 [[routes]] 数组表。"""

        routes: List[Dict[str, Any]] = []
        for raw_route in raw_routes:
            parsed = self._parse_route(raw_route)
            if parsed is None:
                continue
            routes.append(
                {
                    "enabled": parsed.enabled,
                    "name": parsed.name,
                    "target_type": parsed.target_type,
                    "target_id": parsed.target_id,
                    "planner_models": parsed.planner_models,
                    "replyer_models": parsed.replyer_models,
                }
            )
        return routes

    def _migrate_string_routes(self, raw_routes_section: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """兼容上一版字符串 routes.group_routes/private_routes。"""

        routes: List[Dict[str, Any]] = []
        for target_type, key in (("group", "group_routes"), ("private", "private_routes")):
            for raw_rule in self._normalize_string_list(raw_routes_section.get(key, [])):
                route = self._parse_unified_string_route(target_type, raw_rule)
                if route is not None:
                    routes.append(route)
        return routes

    def _migrate_split_stage_routes(self, raw_config: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """把 planner/replyer 分开配置的路由合并成 [[routes]]。"""

        planner = raw_config.get("planner")
        replyer = raw_config.get("replyer")
        if not isinstance(planner, Mapping) and not isinstance(replyer, Mapping):
            return []

        routes: List[Dict[str, Any]] = []
        for target_type, key in (("group", "group_routes"), ("private", "private_routes")):
            merged: Dict[str, Dict[str, List[str]]] = {}

            if isinstance(planner, Mapping):
                for raw_rule in self._normalize_string_list(planner.get(key, [])):
                    parsed = self._parse_stage_only_rule(raw_rule)
                    if parsed is not None:
                        merged.setdefault(parsed[0], {"planner": [], "replyer": []})["planner"] = parsed[1]

            if isinstance(replyer, Mapping):
                for raw_rule in self._normalize_string_list(replyer.get(key, [])):
                    parsed = self._parse_stage_only_rule(raw_rule)
                    if parsed is not None:
                        merged.setdefault(parsed[0], {"planner": [], "replyer": []})["replyer"] = parsed[1]

            for target_id, stage_models in merged.items():
                routes.append(
                    {
                        "enabled": True,
                        "name": f"{self._chat_type_label(target_type)} {target_id}",
                        "target_type": target_type,
                        "target_id": target_id,
                        "planner_models": stage_models["planner"],
                        "replyer_models": stage_models["replyer"],
                    }
                )
        return routes

    def _parse_unified_string_route(self, target_type: str, raw_rule: str) -> Optional[Dict[str, Any]]:
        """解析上一版统一字符串规则：id|planner=a|replyer=b。"""

        parts = [part.strip() for part in str(raw_rule or "").split("|") if part.strip()]
        if not parts:
            return None
        target_id = parts[0].strip()
        fields: Dict[str, str] = {}
        for part in parts[1:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            fields[key.strip().lower()] = value.strip()
        if not target_id:
            return None
        return {
            "enabled": True,
            "name": f"{self._chat_type_label(target_type)} {target_id}",
            "target_type": target_type,
            "target_id": target_id,
            "planner_models": self._normalize_model_names(fields.get("planner", "")),
            "replyer_models": self._normalize_model_names(fields.get("replyer", "")),
        }

    @staticmethod
    def _parse_stage_only_rule(raw_rule: str) -> Optional[Tuple[str, List[str]]]:
        """解析单阶段字符串路由：target:model-a,model-b。"""

        rule_text = str(raw_rule or "").strip()
        if not rule_text:
            return None

        target_id = ""
        model_text = ""
        for delimiter in ("=", ":", "：", "|"):
            if delimiter in rule_text:
                target_id, model_text = rule_text.split(delimiter, 1)
                break

        target_id = target_id.strip()
        models = ModelRouterPlugin._normalize_model_names(model_text)
        if not target_id or not models:
            return None
        return target_id, models

    @staticmethod
    def _normalize_bool(value: Any) -> bool:
        """规范化布尔值。"""

        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    @staticmethod
    def _normalize_string_list(value: Any) -> List[str]:
        """把 WebUI 或 TOML 中的值规范化为字符串列表。"""

        if isinstance(value, str):
            items = [value]
        elif isinstance(value, Iterable):
            items = [str(item).strip() for item in value]
        else:
            items = []
        return [item for item in items if item]

    @staticmethod
    def _md5_session_components(components: Sequence[str]) -> str:
        """按 MaiBot SessionUtils 规则计算 session_id。"""

        return hashlib.md5("_".join(str(item) for item in components).encode()).hexdigest()

    def _get_possible_account_ids(self) -> List[str]:
        """返回已缓存的机器人账号。

        这是 Hook 热路径，必须保持纯内存读取，不能 import 配置模块。
        """

        if not self._cached_account_ids:
            self._refresh_possible_account_ids(allow_import=False)
        return list(self._cached_account_ids)

    def _refresh_possible_account_ids(self, *, allow_import: bool) -> None:
        """刷新机器人账号缓存。

        allow_import=True 只在插件加载/配置热重载时使用；
        Hook 执行期间只允许 allow_import=False，避免再次出现 Hook 超时。
        """

        account_ids: List[str] = []
        config_module = sys.modules.get("src.config.config") or sys.modules.get("config.config")

        if config_module is None and allow_import:
            try:
                import importlib

                config_module = importlib.import_module("src.config.config")
            except Exception:
                config_module = None

        global_config = getattr(config_module, "global_config", None) if config_module is not None else None
        bot_config = getattr(global_config, "bot", None) if global_config is not None else None

        if bot_config is not None:
            qq_account = str(getattr(bot_config, "qq_account", "") or "").strip()
            if qq_account and qq_account != "0":
                account_ids.append(qq_account)

            platforms = getattr(bot_config, "platforms", []) or []
            for item in platforms:
                item_text = str(item or "").strip()
                if ":" not in item_text:
                    continue
                _, account_id = item_text.split(":", 1)
                account_id = account_id.strip()
                if account_id:
                    account_ids.append(account_id)

        self._cached_account_ids = self._unique_non_empty(account_ids)

    @staticmethod
    def _unique_non_empty(values: Iterable[Any]) -> List[str]:
        """去重并删除空字符串。"""

        seen: set[str] = set()
        unique_values: List[str] = []
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            unique_values.append(text)
        return unique_values

    @staticmethod
    def _normalize_target_type(value: Any) -> str:
        """规范化目标类型。"""

        normalized = str(value or "").strip().lower()
        aliases = {
            "group": "group",
            "群": "group",
            "群聊": "group",
            "qq_group": "group",
            "private": "private",
            "私聊": "private",
            "qq": "private",
            "user": "private",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def _normalize_model_names(raw_models: Any) -> List[str]:
        """规范化模型名称列表，去除空项和重复项。"""

        if isinstance(raw_models, str):
            separators_normalized = raw_models.replace("，", ",").replace("；", ",").replace(";", ",")
            raw_items = [item.strip() for item in separators_normalized.split(",")]
        elif isinstance(raw_models, Iterable):
            raw_items = [str(item).strip() for item in raw_models]
        else:
            raw_items = []

        seen: set[str] = set()
        result: List[str] = []
        for model_name in raw_items:
            if not model_name or model_name in seen:
                continue
            seen.add(model_name)
            result.append(model_name)
        return result

    @staticmethod
    def _schema_routes_list_field() -> Dict[str, Any]:
        """构造根级 routes 对象列表字段 Schema。"""

        return {
            "name": "routes",
            "type": "array",
            "label": "路由规则",
            "default": [],
            "description": "一个 QQ号或QQ群号为一组；每组里同时填写 Planner 模型和 Replyer 模型。",
            "ui_type": "list",
            "required": False,
            "hidden": False,
            "disabled": False,
            "order": 0,
            "item_type": "object",
            "item_fields": {
                "enabled": {
                    "type": "boolean",
                    "label": "启用",
                    "default": True,
                    "description": "是否启用这一条路由。",
                    "ui_type": "switch",
                },
                "name": {
                    "type": "string",
                    "label": "路由名称",
                    "default": "示例路由",
                    "description": "只用于日志显示，不影响匹配。",
                    "ui_type": "text",
                    "placeholder": "例如：示例群聊路由",
                },
                "target_type": {
                    "type": "string",
                    "label": "会话类型",
                    "default": "group",
                    "description": "group 表示QQ群聊，private 表示QQ私聊。",
                    "ui_type": "select",
                    "choices": ["group", "private"],
                },
                "target_id": {
                    "type": "string",
                    "label": "QQ号或QQ群号",
                    "default": "",
                    "description": "群聊填写QQ群号；私聊填写对方QQ号。",
                    "ui_type": "text",
                    "placeholder": "例如：123456789",
                },
                "planner_models": {
                    "type": "array",
                    "label": "Planner 模型",
                    "default": [],
                    "description": "Planner 使用的模型名称列表。直接填写 model_config 中已有模型名。",
                    "ui_type": "list",
                    "item_type": "string",
                    "placeholder": "模型2",
                },
                "replyer_models": {
                    "type": "array",
                    "label": "Replyer 模型",
                    "default": [],
                    "description": "Replyer 使用的模型名称列表。直接填写 model_config 中已有模型名。",
                    "ui_type": "list",
                    "item_type": "string",
                    "placeholder": "模型2",
                },
            },
            "min_items": None,
            "max_items": None,
            "placeholder": None,
            "hint": "点击“添加项目”新增一组路由；每组填写一个 QQ号/QQ群号，并在同组内填写 Planner / Replyer 模型。",
            "icon": None,
            "example": [
                {
                    "enabled": True,
                    "name": "示例群聊路由",
                    "target_type": "group",
                    "target_id": "123456789",
                    "planner_models": ["模型2", "模型3"],
                    "replyer_models": ["模型2", "模型3"],
                }
            ],
            "choices": None,
            "min": None,
            "max": None,
            "step": None,
            "pattern": None,
            "max_length": None,
            "input_type": None,
            "rows": 3,
            "group": None,
            "depends_on": None,
            "depends_value": None,
        }

    @staticmethod
    def _schema_field(
        name: str,
        field_type: str,
        label: str,
        default: Any,
        description: str,
        *,
        ui_type: str = "text",
        disabled: bool = False,
        order: int = 0,
    ) -> Dict[str, Any]:
        """构造 WebUI 字段 Schema。"""

        return {
            "name": name,
            "type": field_type,
            "label": label,
            "default": default,
            "description": description,
            "ui_type": ui_type,
            "required": False,
            "hidden": False,
            "disabled": disabled,
            "order": order,
            "item_type": None,
            "item_fields": None,
            "min_items": None,
            "max_items": None,
            "placeholder": None,
            "hint": description,
            "icon": None,
            "example": None,
            "choices": None,
            "min": None,
            "max": None,
            "step": None,
            "pattern": None,
            "max_length": None,
            "input_type": None,
            "rows": 3,
            "group": None,
            "depends_on": None,
            "depends_value": None,
        }

    @staticmethod
    def _stage_label(stage: str) -> str:
        """返回阶段中文名。"""

        return "Planner" if stage == "planner" else "Replyer"

    @staticmethod
    def _chat_type_label(chat_type: str) -> str:
        """返回会话类型中文名。"""

        return "群聊" if chat_type == "group" else "私聊"

    @staticmethod
    def _format_model_chain(models: Sequence[str]) -> str:
        """格式化模型尝试顺序。"""

        return " -> ".join(models) if models else "默认 model_config"


def create_plugin() -> ModelRouterPlugin:
    """创建模型路由插件实例。"""

    return ModelRouterPlugin()
