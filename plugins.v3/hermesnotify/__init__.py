import hashlib
import hmac
import json
import time
from typing import Any, List, Dict, Tuple

import requests

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType


class HermesNotify(_PluginBase):
    # 插件名称
    plugin_name = "Hermes 通知转发"
    # 插件描述
    plugin_desc = "将 MoviePilot 消息事件签名后推送到 Hermes（企业微信/QQ）。"
    # 插件图标
    plugin_icon = "https://hermes-agent.nousresearch.com/img/icon.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "Hermes Agent"
    # 作者主页
    author_url = "https://github.com/NousResearch/hermes-agent"
    # 插件配置项ID前缀
    plugin_config_prefix = "hermes_notify_"
    # 加载顺序
    plugin_order = 29
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _wecom_url = None
    _wecom_secret = None
    _qq_url = None
    _qq_secret = None
    _msgtypes = []

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._msgtypes = config.get("msgtypes") or []
            self._wecom_url = config.get("wecom_url")
            self._qq_url = config.get("qq_url")
            self._wecom_secret = config.get("wecom_secret")
            self._qq_secret = config.get("qq_secret")

    def get_state(self) -> bool:
        return bool(self._enabled
                    and ((self._wecom_url and self._wecom_secret)
                         or (self._qq_url and self._qq_secret)))

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/test",
                "endpoint": self.test_push,
                "methods": ["GET"],
                "summary": "发送一条测试通知到已配置的通道",
            }
        ]

    def test_push(self):
        if not self.get_state():
            return {"success": False, "message": "插件未启用或密钥未配置"}
        import time as _t
        sent = []
        title = "【测试】HermesNotify 端到端"
        content = "MoviePilot→Hermes→企微/QQ 链路验证 " + _t.strftime("%H:%M:%S")
        if self._wecom_url and self._wecom_secret:
            self._push(self._wecom_url, self._wecom_secret, title, content)
            sent.append("wecom")
        if self._qq_url and self._qq_secret:
            self._push(self._qq_url, self._qq_secret, title, content)
            sent.append("qq")
        return {"success": True, "message": "已推送: " + ",".join(sent)}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'wecom_url',
                                            'label': '企业微信路由URL',
                                            'placeholder': 'http://172.17.0.7:8644/webhooks/moviepilot',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'wecom_secret',
                                            'label': '企业微信 HMAC Secret',
                                            'placeholder': '',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'qq_url',
                                            'label': 'QQ路由URL',
                                            'placeholder': 'http://172.17.0.7:8644/webhooks/moviepilot-qq',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'qq_secret',
                                            'label': 'QQ HMAC Secret',
                                            'placeholder': '',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'msgtypes',
                                            'label': '消息类型（留空=全部）',
                                            'items': MsgTypeOptions
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "msgtypes": [],
            "wecom_url": "http://172.17.0.7:8644/webhooks/moviepilot",
            "wecom_secret": "",
            "qq_url": "http://172.17.0.7:8644/webhooks/moviepilot-qq",
            "qq_secret": "",
        }

    def get_page(self) -> List[dict]:
        pass

    @staticmethod
    def _signed_headers(secret: str, payload: bytes) -> dict:
        ts = str(int(time.time()))
        sig = hmac.new(secret.encode(),
                       ts.encode() + b"." + payload,
                       hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": ts,
            "X-Webhook-Signature-V2": sig,
        }

    def _push(self, url: str, secret: str, title: str, content: str) -> None:
        try:
            payload = json.dumps({"title": title, "content": content},
                                 ensure_ascii=False).encode("utf-8")
            res = requests.post(url, data=payload,
                                headers=self._signed_headers(secret, payload),
                                timeout=10)
            if res.status_code == 200:
                logger.info(f"HermesNotify 推送成功: {url}")
            else:
                logger.warn(f"HermesNotify 推送失败 {url}: "
                            f"{res.status_code} {res.text[:100]}")
        except Exception as e:
            logger.error(f"HermesNotify 推送异常 {url}: {e}")

    @eventmanager.register(EventType.NoticeMessage)
    def send(self, event: Event):
        if not self.get_state():
            return
        if not event.event_data:
            return

        msg_body = event.event_data
        msg_type: NotificationType = msg_body.get("type")
        title = msg_body.get("title") or "MoviePilot"
        text = msg_body.get("text") or ""
        link = msg_body.get("link")

        if not msg_body.get("title") and not text:
            return

        if (msg_type and self._msgtypes
                and msg_type.name not in self._msgtypes):
            return

        content = f"{text}\n{link}" if link else text
        channel = msg_type.value if msg_type else ""
        full_title = f"{title}（{channel}）" if channel else title

        if self._wecom_url and self._wecom_secret:
            self._push(self._wecom_url, self._wecom_secret, full_title, content)
        if self._qq_url and self._qq_secret:
            self._push(self._qq_url, self._qq_secret, full_title, content)

    def stop_service(self):
        pass
