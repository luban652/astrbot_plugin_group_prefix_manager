import re
import time
import yaml
from pathlib import Path
from typing import List
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# 尝试导入特定平台的事件类型以进行更安全的类型检查
try:
    from astrbot.api.platform.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
except ImportError:
    AiocqhttpMessageEvent = None

@register("astrbot_plugin_group_prefix_manager", "AstrBotAssistant", "检测群聊中独立的5位数字并将其设置为群名前缀。支持替换已有数字前缀、自动截断超长群名，并提供白名单模式及‘清空’指令。", "1.0.1")
class GroupPrefixManager(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = self.context.get_config()
        # 配置文件路径（AstrBot 固定路径）
        self.config_path = Path(__file__).parent / "config.yml"
        # 预编译正则：匹配独立的5位数字
        self.digit_pattern = re.compile(r"^\d{5}$")
        # 预编译正则：查找群名中存在的5位连续数字
        self.prefix_find_pattern = re.compile(r"\d{5}")
        # 简单的频率限制缓存：group_id -> last_timestamp
        self.cooldown_cache = {}
        self.cooldown_seconds = 3

    def _save_config(self):
        """【核心修复】直接写入文件保存配置，兼容所有 AstrBot v4 版本"""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(self.config, f, allow_unicode=True, sort_keys=False)
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def _is_enabled(self, group_id: str) -> bool:
        """检查当前群组是否启用插件功能"""
        if self.config.get("global_enabled", False):
            return True
        whitelist = self.config.get("whitelist", [])
        return group_id in whitelist

    def _check_cooldown(self, group_id: str) -> bool:
        """检查群组操作频率限制"""
        now = time.time()
        last_time = self.cooldown_cache.get(group_id, 0)
        if now - last_time < self.cooldown_seconds:
            return False
        self.cooldown_cache[group_id] = now
        return True

    @filter.command("prefix_whitelist")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_whitelist(self, event: AstrMessageEvent, action: str):
        """管理数字前缀插件的群组白名单（开启/关闭）"""
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("此指令仅限群聊使用。")
            return

        current_whitelist = list(self.config.get("whitelist", []))
        
        if action == "on":
            if group_id not in current_whitelist:
                current_whitelist.append(group_id)
                self.config["whitelist"] = current_whitelist
                self._save_config()
                yield event.plain_result(f"✅群 {group_id} 已开启数字前缀功能。")
            else:
                yield event.plain_result(f"ℹ️群 {group_id} 已在白名单中。")
        elif action == "off":
            if group_id in current_whitelist:
                current_whitelist.remove(group_id)
                self.config["whitelist"] = current_whitelist
                self._save_config() 
                yield event.plain_result(f"✅群 {group_id} 已关闭数字前缀功能。")
            else:
                yield event.plain_result(f"ℹ️群 {group_id} 不在白名单中。")
        else:
            yield event.plain_result("❌参数错误。请使用: /prefix_whitelist on/off")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_group_message(self, event: AstrMessageEvent):
        """核心逻辑：监听群消息并处理前缀修改"""
        group_id = event.message_obj.group_id
        if not group_id or not self._is_enabled(group_id):
            return

        if self.config.get("admin_only", False):
            if event.get_permission_level() < filter.PermissionType.ADMIN:
                return

        msg_str = event.message_str.strip()

        if msg_str == "清空":
            event.stop_event()
            await self._modify_group_name(event, clear=True)
            return

        if self.digit_pattern.match(msg_str):
            if msg_str in self.config.get("ignore_patterns", []):
                return
            event.stop_event()
            await self._modify_group_name(event, new_prefix=msg_str)

    async def _modify_group_name(self, event: AstrMessageEvent, new_prefix: str = None, clear: bool = False):
        """统一调用平台 API 修改群名"""
        is_cqhttp = False
        if AiocqhttpMessageEvent and isinstance(event, AiocqhttpMessageEvent):
            is_cqhttp = True
        elif event.get_platform_name() == "aiocqhttp":
            is_cqhttp = True

        if not is_cqhttp:
            return

        group_id = event.message_obj.group_id
        
        if not self._check_cooldown(group_id):
            if self.config.get("enable_notify", True):
                await event.send(event.plain_result("⚠️操作太频繁了，请稍后再试。"))
            return

        try:
            client = event.bot
            int_group_id = int(group_id)
            group_info = await client.api.call_action('get_group_info', group_id=int_group_id)
            if not group_info or 'group_name' not in group_info:
                logger.error(f"无法获取群 {group_id} 的信息")
                return
            
            current_name = group_info['group_name']
            old_name_pure = self.prefix_find_pattern.sub("", current_name).strip()
            
            if clear:
                target_name = old_name_pure
            else:
                target_name = f"{new_prefix}{old_name_pure}"
            
            max_len = self.config.get("max_length", 30)
            if len(target_name) > max_len:
                target_name = target_name[:max_len]

            if target_name == current_name:
                return

            await client.api.call_action('set_group_name', group_id=int_group_id, group_name=target_name)
            
            if self.config.get("enable_notify", True):
                await event.send(event.plain_result(""))
                    
        except ValueError:
            logger.error(f"非法的群组 ID: {group_id}")
        except Exception as e:
            logger.error(f"修改群名失败: {str(e)}")
            if self.config.get("enable_notify", True):
                await event.send(event.plain_result("❌修改失败，请确保机器人拥有管理员权限。"))

    async def terminate(self):
        pass
