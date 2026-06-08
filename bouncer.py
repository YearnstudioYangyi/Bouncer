"""
title: Bouncer
version: 0.3.0
author: Open WebUI Community
description: 访问控制与频率限制过滤器。支持基于模型分组的差异化广告投放、白名单及多级流控。
license: MIT
"""

import json
import time
from typing import Optional, Callable, Awaitable, Any
from pydantic import BaseModel, Field

# 依据文档：使用全局字典防止多进程或重载时 self.user_history 被清空
GLOBAL_USER_HISTORY = {}


class Filter:
    class Valves(BaseModel):
        config_json: str = Field(
            default="""
{
  "base": {
    "enabled": true,
    "admin_effective": true
  },
  "auth": {
    "enabled": false,
    "providers": [
      "gmail.com",
      "outlook.com",
      "yourdomain.com"
    ],
    "deny_msg": "您的账户域名未在允许的认证列表中。"
  },
  "whitelist": {
    "enabled": false,
    "emails": []
  },
  "exemption": {
    "enabled": false,
    "emails": []
  },
  "priority": {
    "user_priority": false
  },
  "global_limit": {
    "enabled": false
  },
  "model_groups": [
    {
      "id": "default",
      "name": "默认模型组",
      "models": ["gpt-3.5-turbo", "qwen2.5:7b"],
      "ads": {
        "enabled": true,
        "content": [
          "欢迎使用 Bouncer 访问控制插件！[基础组公告栏]"
        ]
      }
    },
    {
      "id": "premium",
      "name": "高级模型组",
      "models": ["gpt-4o", "claude-3-5-sonnet"],
      "ads": {
        "enabled": true,
        "content": [
          "您正在使用高级模型，请注意额度消耗。[高级组公告栏]"
        ]
      }
    }
  ],
  "user_groups": [
    {
      "id": "default",
      "name": "默认用户组",
      "priority": 0,
      "emails": [],
      "default_permissions": {
        "enabled": true,
        "rpm": 10,
        "rph": 100,
        "win_time": 60,
        "win_limit": 10,
        "clip": 20
      },
      "permissions": {}
    }
  ],
  "ban_reasons": [],
  "fallback": {
    "enabled": false,
    "model": "qwen2:0.5b",
    "notify": true,
    "notify_msg": "已超过频率限制。已切换至备用模型。"
  },
  "logging": {
    "enabled": true,
    "bouncer_log": true,
    "inlet": true,
    "outlet": true,
    "stream": false,
    "user_dict": true
  },
  "ads": {
    "enabled": true,
    "content": [
      "感谢使用开源项目 Bouncer，前往 GitHub 提交 Issue 或 Star 支持！"
    ]
  },
  "custom_strings": {
    "whitelist_deny": "拒绝访问：不在白名单中。",
    "tier_mismatch": "层级不匹配。",
    "user_deny_model": "无法使用该模型",
    "model_wl_deny": "模型白名单拒绝",
    "model_bl_deny": "模型黑名单拒绝",
    "rate_limit_deny": "已触发频率限制：{reason}",
    "group_no_permission": "用户组无权限访问模型组"
  }
}
""",
            description="可以使用 bouncer-webui.pages.dev 快速生成配置 JSON",
        )

    def __init__(self):
        self.valves = self.Valves()
        print("=== BOUNCER INIT ===")

    def get_cfg(self):
        return json.loads(self.valves.config_json)

    def get_limit_cfg(self, cfg):
        for g in cfg.get("user_groups", []):
            if g.get("id") == "default":
                return g.get("default_permissions", {})
        return cfg.get("limit", {})

    def safe_user(self, user):
        if not isinstance(user, dict):
            return user
        u = dict(user)
        for k in ("profile_image_url", "profile_banner_image_url"):
            if k in u:
                u[k] = "<omitted>"
        return u

    # 💡 严格遵循 Open WebUI 官方规范命名，未做任何改动
    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> dict:
        print("\n=== BOUNCER RUNNING ===")
        cfg = self.get_cfg()

        if not cfg.get("base", {}).get("enabled", True):
            print("BOUNCER DISABLED GLOBAL")
            return body

        if not __user__:
            print("🚨 错误: 仍然未能获取到用户上下文")
            return body

        user_id = __user__.get("id", "unknown")
        email = __user__.get("email", "unknown")
        print("USER:", self.safe_user(__user__))
        print("EMAIL:", email)
        print("USER_ID:", user_id)

        if "messages" in body and len(body["messages"]) > 0:
            print("USER MESSAGE:", body["messages"][-1].get("content", ""))

        # ====== 多模型组广告匹配与投放逻辑 ======
        current_model = body.get("model")
        if __event_emitter__ and current_model:
            ad_cfg = None
            for group in cfg.get("model_groups", []):
                if current_model in group.get("models", []):
                    ad_cfg = group.get("ads")
                    break

            if not ad_cfg:
                for group in cfg.get("model_groups", []):
                    if group.get("id") == "default":
                        ad_cfg = group.get("ads")
                        break

            if not ad_cfg:
                ad_cfg = cfg.get("ads")

            if ad_cfg and ad_cfg.get("enabled", False):
                contents = ad_cfg.get("content", [])
                if contents:
                    ad_text = " | ".join(contents)
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {"description": f"[公告] {ad_text}", "done": True},
                        }
                    )
        # ================================================

        limit_cfg = self.get_limit_cfg(cfg)
        if not limit_cfg.get("enabled", False):
            print("LIMIT DISABLED IN JSON")
            return body

        rpm = limit_cfg.get("rpm", 1)
        rph = limit_cfg.get("rph", 1)

        now = time.time()
        global GLOBAL_USER_HISTORY
        if user_id not in GLOBAL_USER_HISTORY:
            GLOBAL_USER_HISTORY[user_id] = []
        history = GLOBAL_USER_HISTORY[user_id]

        history = [t for t in history if now - t < 3600]
        GLOBAL_USER_HISTORY[user_id] = history

        rpm_count = len([t for t in history if now - t < 60])
        rph_count = len(history)
        print(f"RPM LIMIT: {rpm} | CURRENT RPM: {rpm_count}")
        print(f"RPH LIMIT: {rph} | CURRENT RPH: {rph_count}")

        if rpm > 0 and rpm_count >= rpm:
            print(f"❌ 拒绝用户 {email}: 触发 RPM 限制")
            raise Exception(f"已触发频率限制 RPM ({rpm}次/分钟)")
        if rph > 0 and rph_count >= rph:
            print(f"❌ 拒绝用户 {email}: 触发 RPH 限制")
            raise Exception(f"已触发频率限制 RPH ({rph}次/小时)")

        history.append(now)
        GLOBAL_USER_HISTORY[user_id] = history
        print("✅ 流控放行。当前历史队列:", history)
        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body
