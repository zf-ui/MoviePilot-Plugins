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
    plugin_version = "1.6"
    # 插件作者
    plugin_author = "zf-ui"
    # 作者主页
    author_url = "https://github.com/zf-ui"
    # 插件配置项ID前缀
    plugin_config_prefix = "hermes_notify_"
    # 加载顺序
    plugin_order = 29
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _wecom_enabled = True
    _qq_enabled = True
    _wecom_url = None
    _wecom_secret = None
    _qq_url = None
    _qq_secret = None
    _msgtypes = []

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._msgtypes = config.get("msgtypes") or []
            self._wecom_enabled = config.get("wecom_enabled", True)
            self._qq_enabled = config.get("qq_enabled", True)
            self._wecom_url = config.get("wecom_url")
            self._qq_url = config.get("qq_url")
            self._wecom_secret = config.get("wecom_secret")
            self._qq_secret = config.get("qq_secret")

    def get_state(self) -> bool:
        return bool(self._enabled
                    and ((self._wecom_enabled and self._wecom_url and self._wecom_secret)
                         or (self._qq_enabled and self._qq_url and self._qq_secret)))

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/test",
                "endpoint": self.test_push,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "发送一条测试通知到已配置的通道",
            },
            {
                "path": "/test/wecom",
                "endpoint": self.test_wecom,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "向企业微信通道发送测试通知",
            },
            {
                "path": "/test/qq",
                "endpoint": self.test_qq,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "向 QQ 通道发送测试通知",
            },
        ]

    def _send_test(self, url: str, secret: str, channel: str) -> Dict[str, Any]:
        title = "【测试】HermesNotify"
        content = f"MoviePilot→Hermes→{channel} 独立按钮链路验证 " + time.strftime("%H:%M:%S")
        ok = self._push(url, secret, title, content)
        if ok is None:
            return {"success": False, "message": f"{channel} 通道未配置", "data": None}
        return {"success": bool(ok), "message": f"{channel} 推送{'成功' if ok else '失败'}", "data": None}

    def test_wecom(self):
        if not self._wecom_enabled:
            return {"success": False, "message": "企业微信通道已被关闭（wecom_enabled）", "data": None}
        if not self._wecom_url or not self._wecom_secret:
            return {"success": False, "message": "企业微信通道未配置", "data": None}
        return self._send_test(self._wecom_url, self._wecom_secret, "企业微信")

    def test_qq(self):
        if not self._qq_enabled:
            return {"success": False, "message": "QQ 通道已被关闭（qq_enabled）", "data": None}
        if not self._qq_url or not self._qq_secret:
            return {"success": False, "message": "QQ 通道未配置", "data": None}
        return self._send_test(self._qq_url, self._qq_secret, "QQ")

    def test_push(self):
        if not self.get_state():
            return {"success": False, "message": "插件未启用或密钥未配置", "data": None}
        sent = []
        if self._wecom_enabled and self._wecom_url and self._wecom_secret:
            self._push(self._wecom_url, self._wecom_secret,
                      "【测试】HermesNotify 端到端",
                      "MoviePilot→Hermes→企微/QQ 链路验证 " + time.strftime("%H:%M:%S"))
            sent.append("企业微信")
        if self._qq_enabled and self._qq_url and self._qq_secret:
            self._push(self._qq_url, self._qq_secret,
                       "【测试】HermesNotify 端到端",
                       "MoviePilot→Hermes→企微/QQ 链路验证 " + time.strftime("%H:%M:%S"))
            sent.append("QQ")
        if not sent:
            return {"success": False, "message": "没有已开启且已配置的通道", "data": None}
        return {"success": True, "message": "已推送到: " + "、".join(sent), "data": None}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })

        def _btn(text: str, icon: str, color: str, api: str, okmsg: str, failmsg: str) -> dict:
            onclick = (
                "function(e) { window.MoviePilotAPI.get('" + api + "')"
                ".then(function(r) { if (r && r.success === false) {"
                " alert(r.message || '" + failmsg + "') } else {"
                " alert('" + okmsg + "') } })"
                ".catch(function(err) { console.error(err); alert('" + failmsg + "') }) }"
            )
            return {
                'component': 'VBtn',
                'props': {
                    'color': color,
                    'variant': 'tonal',
                    'prepend-icon': icon,
                    'block': True,
                    'onclick': onclick,
                },
                'text': text,
            }

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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'wecom_enabled',
                                            'label': '启用企业微信通道',
                                            'color': 'success'
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'qq_enabled',
                                            'label': '启用QQ通道',
                                            'color': 'primary'
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
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    _btn('测试企业微信', 'mdi-wechat', 'success',
                                         'plugin/HermesNotify/test/wecom',
                                         '已发送到企业微信', '企业微信发送失败'),
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    _btn('测试QQ', 'mdi-qqchat', 'primary',
                                         'plugin/HermesNotify/test/qq',
                                         '已发送到QQ', 'QQ发送失败'),
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    _btn('测试全部通道', 'mdi-check-all', 'warning',
                                         'plugin/HermesNotify/test',
                                         '已发送到全部通道', '发送失败'),
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "msgtypes": [],
            "wecom_enabled": True,
            "qq_enabled": False,
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

    def _push(self, url: str, secret: str, title: str, content: str) -> bool | None:
        if not url or not secret:
            return None
        try:
            payload = json.dumps({"title": title, "content": content},
                                 ensure_ascii=False).encode("utf-8")
            res = requests.post(url, data=payload,
                                headers=self._signed_headers(secret, payload),
                                timeout=10)
            if res.status_code == 200:
                logger.info(f"HermesNotify 推送成功: {url}")
                return True
            logger.warn(f"HermesNotify 推送失败 {url}: "
                        f"{res.status_code} {res.text[:100]}")
            return False
        except Exception as e:
            logger.error(f"HermesNotify 推送异常 {url}: {e}")
            return False

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
        # 标题保持原样，不再拼接消息类型后缀（如「（插件）」）
        full_title = title

        if self._wecom_enabled and self._wecom_url and self._wecom_secret:
            self._push(self._wecom_url, self._wecom_secret, full_title, content)
        if self._qq_enabled and self._qq_url and self._qq_secret:
            self._push(self._qq_url, self._qq_secret, full_title, content)

    def stop_service(self):
        pass
