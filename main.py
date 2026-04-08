import re
import time
import json
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
        # 配置文件路径
        self.config_path = Path(__file__).parent / "config.json"
        # 加载配置
        self.config = self._load_config()
        # 合并默认配置
        self._merge_default_config()
        # 预编译正则
        self.digit_pattern = re.compile(r"^\d{5}$")
        self.prefix_find_pattern = re.compile(r"\d{5}")
        # 频率限制
        self.cooldown_cache = {}
        self.cooldown_seconds = 3

    def _load_config(self):
        """加载配置文件"""
        default_config = {
            "global_enabled": False,
            "whitelist": [],
            "max_length": 30,
            "enable_notify": True,
            "admin_only": False,
            "ignore_patterns": []
        }
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    # 合并，确保所有字段都存在
                    for key, value in default_config.items():
                        if key not in config:
                            config[key] = value
                    return config
            except Exception as e:
                logger.error(f"加载配置失败: {e}")
                return default_config
        return default_config

    def _merge_default_config(self):
        """合并默认配置"""
        default_config = {
            "global_enabled": False,
            "whitelist": [],
            "max_length": 30,
            "enable_notify": True,
            "admin_only": False,
            "ignore_patterns": []
        }
        for key, value in default_config.items():
            if key not in self.config:
                self.config[key] = value

    def _save_config(self):
        """保存配置到文件"""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            logger.info(f"配置已保存: {self.config_path}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def _is_enabled(self, group_id: str) -> bool:
        """检查当前群组是否启用插件功能"""
        if self.config.get("global_enabled", False):
            return True
        whitelist = self.config.get("whitelist", [])
        # 确保 group_id 是字符串类型进行比较
        return str(group_id) in whitelist

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
            yield event.plain_result("仅限群聊")
            return

        group_id_str = str(group_id)
        current_whitelist = list(self.config.get("whitelist", []))
        
        if action == "on":
            if group_id_str not in current_whitelist:
                current_whitelist.append(group_id_str)
                self.config["whitelist"] = current_whitelist
                self._save_config()
                yield event.plain_result(f"✅已开启")
                logger.info(f"群 {group_id_str} 已加入白名单")
            else:
                yield event.plain_result(f"ℹ️已开启")
        elif action == "off":
            if group_id_str in current_whitelist:
                current_whitelist.remove(group_id_str)
                self.config["whitelist"] = current_whitelist
                self._save_config()
                yield event.plain_result(f"✅已关闭")
                logger.info(f"群 {group_id_str} 已移出白名单")
            else:
                yield event.plain_result(f"ℹ️已关闭")
        else:
            yield event.plain_result("❌参数错误")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_group_message(self, event: AstrMessageEvent):
        """核心逻辑：监听群消息并处理前缀修改"""
        group_id = event.message_obj.group_id
        if not group_id:
            return
        
        group_id_str = str(group_id)
        
        # 检查是否启用
        if not self._is_enabled(group_id_str):
            return

        # 检查权限
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

        group_id = str(event.message_obj.group_id)
        
        if not self._check_cooldown(group_id):
            if self.config.get("enable_notify", True):
                await event.send(event.plain_result("⏳稍后再试"))
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
            
            if self.config.get("enable_notify", True) and not clear:
                await event.send(event.plain_result(""))
                    
        except ValueError:
            logger.error(f"非法的群组 ID: {group_id}")
        except Exception as e:
            logger.error(f"修改群名失败: {str(e)}")
            if self.config.get("enable_notify", True):
                await event.send(event.plain_result("❌失败"))

    async def terminate(self):
        pass
