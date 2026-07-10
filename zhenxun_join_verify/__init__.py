import asyncio
import contextlib
import json
import os
import random
import time
from typing import Any

from nonebot import get_driver, on_message, on_notice
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    GroupAdminNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
)
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot_plugin_alconna import Alconna, Args, At, Match, UniMessage, on_alconna
from nonebot_plugin_alconna.uniseg.tools import reply_fetch
from nonebot_plugin_uninfo import Uninfo

from zhenxun.configs.config import Config
from zhenxun.configs.path_config import DATA_PATH
from zhenxun.configs.utils import PluginExtraData, RegisterConfig, Task
from zhenxun.services.log import logger
from zhenxun.utils.common_utils import CommonUtils
from zhenxun.utils.message import MessageUtils
from zhenxun.utils.rules import admin_check

PLUGIN_MODULE = "zhenxun_join_verify"
DATA_DIR = DATA_PATH / PLUGIN_MODULE
NOTIFY_USERS_FILE = DATA_DIR / "notify_users.json"
PENDING_VERIFICATIONS_FILE = DATA_DIR / "pending_verifications.json"
driver = get_driver()

__plugin_meta__ = PluginMetadata(
    name="进群验证",
    description="新人进群需在规定时间内回答验证题目，否则将被踢出群聊（被动插件，默认关闭）",
    usage="""
    新人进群时，若群内开启了此被动任务，机器人会自动@新人发送一道验证题目。
    新人直接回复纯数字答案即可。答错指定次数或超时未答，将被自动移出群聊。
    
    开启/关闭指令（需群管/群主）：
    开启被动 进群验证
    关闭被动 进群验证

    提醒用户管理（需群管/群主）：
    进群验证提醒列表
    添加进群验证提醒 QQ号
    移除进群验证提醒 QQ号

    中断处理：
    回复验证消息并发送「取消验证」即可中断验证流程（需群管/群主）
    """.strip(),
    extra=PluginExtraData(
        author="AIGC_Hychan2333",
        version="1.2",
        tasks=[
            Task(
                module=PLUGIN_MODULE,
                name="进群验证",
                create_status=False,
                default_status=False,
            )
        ],
        configs=[
            RegisterConfig(
                module=PLUGIN_MODULE,
                key="timeout",
                value=600,
                default_value=600,
                type=int,
                help="验证超时时间(秒), 默认600秒(10分钟)",
            ),
            RegisterConfig(
                module=PLUGIN_MODULE,
                key="max_retries",
                value=3,
                default_value=3,
                type=int,
                help="允许答错的最大次数",
            ),
            RegisterConfig(
                module=PLUGIN_MODULE,
                key="notify_when_no_permission",
                value=True,
                default_value=True,
                type=bool,
                help="Bot没有群管理权限时是否私聊通知提醒用户",
            ),
            RegisterConfig(
                module=PLUGIN_MODULE,
                key="permission_notify_cooldown",
                value=600,
                default_value=600,
                type=int,
                help="Bot无管理权限提醒冷却时间(秒)，避免重复刷屏",
            ),
        ],
    ).to_dict(),
)

verification_tasks: dict[tuple[int, int], dict[str, Any]] = {}
verification_message_index: dict[str, tuple[int, int]] = {}
permission_notify_time: dict[int, float] = {}
welcome_reserved_until: dict[int, float] = {}


def _ensure_notify_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not NOTIFY_USERS_FILE.exists():
        NOTIFY_USERS_FILE.write_text("{}", encoding="utf-8")


def _ensure_pending_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PENDING_VERIFICATIONS_FILE.exists():
        PENDING_VERIFICATIONS_FILE.write_text("{}", encoding="utf-8")


def _load_notify_users() -> dict[str, list[str]]:
    _ensure_notify_file()
    try:
        data = json.loads(NOTIFY_USERS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("读取进群验证提醒用户配置失败", PLUGIN_MODULE, e=e)
        return {}
    return data if isinstance(data, dict) else {}


def _save_notify_users(data: dict[str, list[str]]) -> None:
    _ensure_notify_file()
    NOTIFY_USERS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get_notify_users(group_id: int | str) -> list[str]:
    users = _load_notify_users().get(str(group_id), [])
    return [str(user_id) for user_id in users]


def _add_notify_user(group_id: int | str, user_id: int | str) -> bool:
    data = _load_notify_users()
    group_key = str(group_id)
    user_key = str(user_id)
    data.setdefault(group_key, [])
    if user_key in data[group_key]:
        return False
    data[group_key].append(user_key)
    _save_notify_users(data)
    return True


def _remove_notify_user(group_id: int | str, user_id: int | str) -> bool:
    data = _load_notify_users()
    group_key = str(group_id)
    user_key = str(user_id)
    if user_key not in data.get(group_key, []):
        return False
    data[group_key].remove(user_key)
    if not data[group_key]:
        data.pop(group_key, None)
    _save_notify_users(data)
    return True


def _get_config_value(key: str, default: Any) -> Any:
    return Config.get_config(PLUGIN_MODULE, key, default)


def _pending_key(group_id: int, user_id: int) -> str:
    return f"{group_id}:{user_id}"


def _load_pending_data() -> dict[str, dict[str, Any]]:
    _ensure_pending_file()
    try:
        data = json.loads(PENDING_VERIFICATIONS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("读取进群验证待验证状态失败", PLUGIN_MODULE, e=e)
        return {}
    return data if isinstance(data, dict) else {}


def _save_pending_verifications() -> None:
    _ensure_pending_file()
    data: dict[str, dict[str, Any]] = {}
    for (group_id, user_id), task_info in verification_tasks.items():
        data[_pending_key(group_id, user_id)] = {
            "group_id": group_id,
            "user_id": user_id,
            "answer": task_info.get("answer"),
            "retries": task_info.get("retries", 0),
            "max_retries": task_info.get("max_retries"),
            "expire_at": task_info.get("expire_at"),
            "message_ids": [str(mid) for mid in task_info.get("message_ids", [])],
            "welcome_reserved": bool(task_info.get("welcome_reserved")),
        }
    try:
        PENDING_VERIFICATIONS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("保存进群验证待验证状态失败", PLUGIN_MODULE, e=e)


def _load_pending_verifications_to_memory() -> None:
    data = _load_pending_data()
    now = time.time()
    changed = False
    for item in data.values():
        try:
            group_id = int(item["group_id"])
            user_id = int(item["user_id"])
            expire_at = float(item.get("expire_at") or now)
            max_retries = int(item["max_retries"])
            answer = int(item["answer"])
        except (KeyError, TypeError, ValueError):
            changed = True
            continue

        key = (group_id, user_id)
        if key in verification_tasks:
            continue
        message_ids = [str(mid) for mid in item.get("message_ids", []) if mid]
        verification_tasks[key] = {
            "answer": answer,
            "retries": int(item.get("retries", 0) or 0),
            "max_retries": max_retries,
            "expire_at": expire_at,
            "message_ids": message_ids,
            "welcome_reserved": bool(item.get("welcome_reserved")),
            "restored": True,
        }
        for message_id in message_ids:
            verification_message_index[message_id] = key
    if changed:
        _save_pending_verifications()


def _extract_receipt_message_ids(receipt: Any) -> list[str]:
    msg_ids = getattr(receipt, "msg_ids", None)
    if not msg_ids:
        return []

    result: list[str] = []
    for msg_id_info in msg_ids:
        message_id = None
        if isinstance(msg_id_info, dict):
            message_id = msg_id_info.get("message_id")
        else:
            message_id = getattr(msg_id_info, "message_id", None)
        if message_id is not None:
            result.append(str(message_id))
    return result


def _bind_verification_message_ids(
    key: tuple[int, int],
    task_info: dict[str, Any],
    receipt: Any,
) -> None:
    message_ids = _extract_receipt_message_ids(receipt)
    if not message_ids:
        return
    task_info["message_ids"] = message_ids
    for message_id in message_ids:
        verification_message_index[message_id] = key
    _save_pending_verifications()


def _pop_verification_task(key: tuple[int, int]) -> dict[str, Any] | None:
    task_info = verification_tasks.pop(key, None)
    if not task_info:
        return None

    for message_id in task_info.get("message_ids", []):
        verification_message_index.pop(str(message_id), None)
    for message_id, message_key in list(verification_message_index.items()):
        if message_key == key:
            verification_message_index.pop(message_id, None)
    _save_pending_verifications()
    return task_info


def _cancel_timeout_task(task_info: dict[str, Any] | None) -> None:
    if not task_info:
        return
    task = task_info.get("task")
    if task:
        task.cancel()


def _schedule_timeout_task(bot: Bot, key: tuple[int, int], task_info: dict[str, Any]):
    if task_info.get("task"):
        return
    expire_at = float(task_info.get("expire_at") or time.time())
    timeout = max(1, int(expire_at - time.time()))
    task_info["task"] = asyncio.create_task(timeout_kick(bot, key[0], key[1], timeout))


_load_pending_verifications_to_memory()


async def _bot_has_group_admin_permission(bot: Bot, group_id: int) -> bool:
    try:
        member_info = await bot.get_group_member_info(
            group_id=group_id,
            user_id=int(bot.self_id),
            no_cache=True,
        )
    except Exception as e:
        logger.error(
            f"获取Bot群权限失败，跳过进群验证: {e}",
            PLUGIN_MODULE,
            target=group_id,
            e=e,
        )
        return False
    return member_info.get("role") in {"owner", "admin"}


async def _is_verification_admin(
    bot: Bot,
    event: GroupMessageEvent,
    session: Uninfo,
) -> bool:
    if await admin_check(5)(bot, event, session):
        return True
    try:
        member_info = await bot.get_group_member_info(
            group_id=event.group_id,
            user_id=event.user_id,
            no_cache=True,
        )
    except Exception as e:
        logger.error(
            f"获取手动取消验证用户权限失败: {e}",
            PLUGIN_MODULE,
            target=event.group_id,
            e=e,
        )
        return False
    return member_info.get("role") in {"owner", "admin"}


async def _notify_no_permission(
    bot: Bot,
    group_id: int,
    user_id: int,
    reason: str = "Bot不是群主/管理员，无法踢出未通过验证的用户",
) -> None:
    if not _get_config_value("notify_when_no_permission", True):
        return

    cooldown = int(_get_config_value("permission_notify_cooldown", 600) or 0)
    now = time.time()
    if cooldown > 0 and now - permission_notify_time.get(group_id, 0) < cooldown:
        logger.info(
            f"群 {group_id} Bot无管理权限提醒仍在冷却中，跳过通知",
            PLUGIN_MODULE,
            target=group_id,
        )
        return

    notify_users = _get_notify_users(group_id)
    if not notify_users:
        logger.warning(
            f"群 {group_id} 未配置提醒用户，无法发送Bot无管理权限通知",
            PLUGIN_MODULE,
            target=group_id,
        )
        return

    private_message = (
        f"【进群验证未启动】\n"
        f"群号: {group_id}\n"
        f"新成员: {user_id}\n"
        f"原因: {reason}\n\n"
        f"请先将Bot设置为群管理员，否则无法在验证失败/超时时移出用户。"
    )
    sent = False
    for uid in notify_users:
        try:
            await bot.send_private_msg(user_id=int(uid), message=private_message)
            sent = True
            logger.info(
                f"已私聊通知用户 {uid} 处理群 {group_id} 的Bot管理权限问题",
                PLUGIN_MODULE,
                target=group_id,
            )
        except Exception as e:
            logger.error(
                f"私聊通知用户 {uid} 失败: {e}",
                PLUGIN_MODULE,
                target=group_id,
                e=e,
            )
    if sent:
        permission_notify_time[group_id] = now


async def _cancel_group_verifications_for_no_permission(
    bot: Bot,
    group_id: int,
    reason: str,
) -> None:
    cancelled_users: list[int] = []
    for key, task_info in list(verification_tasks.items()):
        task_group_id, task_user_id = key
        if task_group_id != group_id:
            continue
        task_info = _pop_verification_task(key)
        if not task_info:
            continue
        task = task_info.get("task")
        if task:
            task.cancel()
        cancelled_users.append(task_user_id)

    _release_reserved_group_welcome_cooldown(group_id)
    if not cancelled_users:
        return

    logger.info(
        f"群 {group_id} 因Bot无管理权限取消 {len(cancelled_users)} 个进群验证",
        PLUGIN_MODULE,
        target=group_id,
    )
    try:
        await bot.send_group_msg(
            group_id=group_id,
            message="管理员权限已被取消...流程中断...",
        )
    except Exception as e:
        logger.error(
            f"发送进群验证停止提示失败: {e}",
            PLUGIN_MODULE,
            target=group_id,
            e=e,
        )
    await _notify_no_permission(bot, group_id, cancelled_users[0], reason)


def is_pending_verification(group_id: int | str, user_id: int | str) -> bool:
    try:
        key = (int(group_id), int(user_id))
    except (TypeError, ValueError):
        return False
    return key in verification_tasks


def _welcome_cooldown_keys(group_id: int | str) -> set[int | str]:
    keys: set[int | str] = {group_id, str(group_id)}
    try:
        keys.add(int(group_id))
    except (TypeError, ValueError):
        pass
    return keys


def _reserve_group_welcome_cooldown(group_id: int | str, cd_time: int) -> bool:
    try:
        from zhenxun.builtin_plugins.platform.qq.group_handle.data_source import (
            GroupManager,
        )

        for key in _welcome_cooldown_keys(group_id):
            GroupManager._flmt.start_cd(key, cd_time)
        welcome_reserved_until[int(group_id)] = time.time() + cd_time
        return True
    except Exception as e:
        logger.error("预占入群欢迎冷却失败", PLUGIN_MODULE, e=e)
        return False


def _release_reserved_group_welcome_cooldown(group_id: int | str) -> None:
    try:
        group_key = int(group_id)
    except (TypeError, ValueError):
        return
    reserved_until = welcome_reserved_until.get(group_key)
    if not reserved_until or time.time() >= reserved_until:
        return

    try:
        from zhenxun.builtin_plugins.platform.qq.group_handle.data_source import (
            GroupManager,
        )

        for key in _welcome_cooldown_keys(group_id):
            next_time = GroupManager._flmt.next_time.get(key, 0)
            if next_time <= reserved_until + 1:
                GroupManager._flmt.next_time[key] = 0
        welcome_reserved_until.pop(group_key, None)
    except Exception as e:
        logger.error("释放入群欢迎冷却失败", PLUGIN_MODULE, e=e)


def _start_group_welcome_cooldown(group_id: int | str) -> None:
    from zhenxun.builtin_plugins.platform.qq.group_handle.data_source import (
        GroupManager,
    )

    for key in _welcome_cooldown_keys(group_id):
        GroupManager._flmt.start_cd(key)
    with contextlib.suppress(TypeError, ValueError):
        welcome_reserved_until.pop(int(group_id), None)


def _can_send_group_welcome(group_id: int | str) -> bool:
    from zhenxun.builtin_plugins.platform.qq.group_handle.data_source import (
        GroupManager,
    )

    return all(GroupManager._flmt.check(key) for key in _welcome_cooldown_keys(group_id))


def _build_welcome_message(group_id: int | str, user_id: int | str) -> UniMessage:
    from zhenxun.builtin_plugins.admin.welcome_message.data_source import BASE_PATH
    from zhenxun.builtin_plugins.platform.qq.group_handle.data_source import (
        DEFAULT_IMAGE_PATH,
    )
    import ujson as json

    file = BASE_PATH / "qq" / str(group_id) / "text.json"
    if file.exists():
        with file.open(encoding="utf8") as f:
            json_data = json.load(f)
        enabled_keys = [k for k in json_data if json_data[k]["status"]]
        if enabled_keys:
            welcome_data = json_data[random.choice(enabled_keys)]
            msg_list = UniMessage().load(welcome_data["message"])
            if welcome_data["at"]:
                msg_list.insert(0, At("user", str(user_id)))
            if msg_list:
                return MessageUtils.build_message(msg_list)

    image = DEFAULT_IMAGE_PATH / random.choice(os.listdir(DEFAULT_IMAGE_PATH))
    return MessageUtils.build_message(["欢迎新人~", image])


def generate_math_problem() -> tuple[str, int]:
    a = random.randint(1, 50)
    b = random.randint(1, 50)
    op = random.choice(["+", "-"])

    if op == "+":
        ans = a + b
    else:
        if a < b:
            a, b = b, a
        ans = a - b

    return f"{a} {op} {b} = ?", ans


async def timeout_kick(bot: Bot, group_id: int, user_id: int, timeout: int):
    await asyncio.sleep(timeout)
    key = (group_id, user_id)
    if key in verification_tasks:
        _pop_verification_task(key)
        try:
            await bot.set_group_kick(
                group_id=group_id, user_id=user_id, reject_add_request=False
            )
            await MessageUtils.build_message(
                f"用户 [{user_id}] 验证超时，已被移出群聊."
            ).send()
            logger.info(
                f"用户 {user_id} 在群 {group_id} 验证超时，已被自动踢出。",
                PLUGIN_MODULE,
            )
        except Exception as e:
            _release_reserved_group_welcome_cooldown(group_id)
            logger.error(
                f"踢出用户 {user_id} 失败 (可能已退群或权限不足): {e}",
                PLUGIN_MODULE,
            )
            await _notify_no_permission(
                bot,
                group_id,
                user_id,
                f"验证超时后踢出用户失败，可能是Bot没有群管理权限: {e}",
            )


@driver.on_bot_connect
async def _restore_pending_verifications(bot: Bot):
    _load_pending_verifications_to_memory()
    restored_count = 0
    for key, task_info in list(verification_tasks.items()):
        _schedule_timeout_task(bot, key, task_info)
        restored_count += 1
    if restored_count:
        logger.info(
            f"已恢复 {restored_count} 个进群验证待验证状态",
            PLUGIN_MODULE,
        )


increase_notice = on_notice(priority=0, block=False)


show_notify_users = on_alconna(
    Alconna("进群验证提醒列表"),
    rule=admin_check(5),
    priority=5,
    block=True,
)


@show_notify_users.handle()
async def _(session: Uninfo):
    if not session.group:
        await MessageUtils.build_message("该命令仅支持群聊使用").send()
        return

    group_id = session.group.id
    notify_users = _get_notify_users(group_id)
    if not notify_users:
        await MessageUtils.build_message(
            "当前群未设置进群验证提醒用户\n"
            "使用「添加进群验证提醒 QQ号」添加"
        ).send()
        return

    msg = ["【进群验证提醒用户】"]
    msg.extend(f"{idx}. {uid}" for idx, uid in enumerate(notify_users, 1))
    await MessageUtils.build_message("\n".join(msg)).send()


add_notify_user = on_alconna(
    Alconna("添加进群验证提醒", Args["user_id", int]),
    rule=admin_check(5),
    priority=5,
    block=True,
)


@add_notify_user.handle()
async def _(session: Uninfo, user_id: Match[int]):
    if not session.group:
        await MessageUtils.build_message("该命令仅支持群聊使用").send()
        return
    if not user_id.available:
        await MessageUtils.build_message(
            "请提供要添加的QQ号"
        ).send()
        return

    group_id = session.group.id
    uid = str(user_id.result)
    if _add_notify_user(group_id, uid):
        logger.info(
            f"群 {group_id} 添加进群验证提醒用户: {uid}",
            PLUGIN_MODULE,
            target=group_id,
        )
        await MessageUtils.build_message(f"✅ 已添加进群验证提醒用户: {uid}").send()
    else:
        await MessageUtils.build_message("❌ 该用户已在提醒列表中").send()


remove_notify_user = on_alconna(
    Alconna("移除进群验证提醒", Args["user_id", int]),
    rule=admin_check(5),
    priority=5,
    block=True,
)


@remove_notify_user.handle()
async def _(session: Uninfo, user_id: Match[int]):
    if not session.group:
        await MessageUtils.build_message("该命令仅支持群聊使用").send()
        return
    if not user_id.available:
        await MessageUtils.build_message(
            "请提供要移除的QQ号"
        ).send()
        return

    group_id = session.group.id
    uid = str(user_id.result)
    if _remove_notify_user(group_id, uid):
        logger.info(
            f"群 {group_id} 移除进群验证提醒用户: {uid}",
            PLUGIN_MODULE,
            target=group_id,
        )
        await MessageUtils.build_message(f"✅ 已移除进群验证提醒用户: {uid}").send()
    else:
        await MessageUtils.build_message("❌ 该用户不在提醒列表中").send()


@increase_notice.handle()
async def _(bot: Bot, event: GroupIncreaseNoticeEvent | GroupAdminNoticeEvent):
    if isinstance(event, GroupAdminNoticeEvent):
        if event.sub_type == "unset" and event.user_id == int(bot.self_id):
            await _cancel_group_verifications_for_no_permission(
                bot,
                event.group_id,
                "Bot管理权限被撤销，已取消当前群正在进行的进群验证",
            )
        return

    if not isinstance(event, GroupIncreaseNoticeEvent):
        return

    logger.info(
        f"进群验证触发: group={event.group_id}, user={event.user_id}, sub_type={event.sub_type}"
    )

    if event.sub_type not in ["approve", "invite"]:
        logger.info("sub_type 不符合，跳过")
        return

    if event.user_id == int(bot.self_id):
        logger.info("机器人自己进群，跳过")
        return

    group_id = event.group_id
    user_id = event.user_id
    key = (group_id, user_id)

    if key in verification_tasks:
        logger.info("已在验证列表中，跳过")
        return

    # 检查该群是否开启了此被动任务
    is_blocked = await CommonUtils.task_is_block(bot, PLUGIN_MODULE, str(group_id))
    logger.info(f"被动任务状态: is_blocked={is_blocked}")
    if is_blocked:
        logger.info("被动任务未开启，跳过")
        return

    if not await _bot_has_group_admin_permission(bot, group_id):
        _release_reserved_group_welcome_cooldown(group_id)
        await _notify_no_permission(bot, group_id, user_id)
        return

    problem, answer = generate_math_problem()
    timeout = _get_config_value("timeout", 600)
    max_retries = _get_config_value("max_retries", 3)
    expire_at = time.time() + timeout

    welcome_reserved = False
    if not await CommonUtils.task_is_block(bot, "group_welcome", str(group_id)):
        welcome_reserved = _reserve_group_welcome_cooldown(group_id, timeout + 30)

    verification_tasks[key] = {
        "answer": answer,
        "retries": 0,
        "max_retries": max_retries,
        "expire_at": expire_at,
        "welcome_reserved": welcome_reserved,
    }
    _save_pending_verifications()

    task = asyncio.create_task(timeout_kick(bot, group_id, user_id, timeout))
    verification_tasks[key]["task"] = task

    msg = UniMessage().at(str(user_id)) + (
        f" 为了防止坏人，请在{timeout // 60}分钟内回答以下题目：\n"
        f"[ {problem} ]\n"
        f"请直接回复纯数字答案。答错{max_retries}次或超时将被移出群聊。"
    )

    logger.info(f"准备发送验证消息给群 {group_id} 的用户 {user_id}")
    receipt = await msg.send()
    _bind_verification_message_ids(key, verification_tasks[key], receipt)
    logger.info("验证消息发送完成")


def is_in_verification(event: GroupMessageEvent) -> bool:
    return (event.group_id, event.user_id) in verification_tasks


def _resolve_verification_key_from_reply(
    bot: Bot,
    event: GroupMessageEvent,
    reply: Any,
    *,
    require_pending: bool = True,
) -> tuple[int, int] | None:
    key = verification_message_index.get(str(reply.id))
    if key and (not require_pending or key in verification_tasks):
        return key

    origin = getattr(reply, "origin", None)
    sender = getattr(origin, "sender", None)
    sender_id = getattr(sender, "user_id", None)
    if sender_id is not None:
        try:
            if int(sender_id) != int(bot.self_id):
                return None
        except (TypeError, ValueError):
            return None

    for segment in getattr(reply, "msg", []) or []:
        if getattr(segment, "type", None) != "at":
            continue
        user_id = getattr(segment, "data", {}).get("qq")
        if not user_id or user_id == "all":
            continue
        try:
            key = (event.group_id, int(user_id))
        except (TypeError, ValueError):
            continue
        if not require_pending or key in verification_tasks:
            return key
    return None


async def is_manual_cancel_verification(
    bot: Bot,
    event: GroupMessageEvent,
    session: Uninfo,
) -> bool:
    if event.get_plaintext().strip() != "取消验证":
        return False
    reply = await reply_fetch(event, bot)
    if not reply or not _resolve_verification_key_from_reply(
        bot, event, reply, require_pending=False
    ):
        return False
    return await _is_verification_admin(bot, event, session)


manual_cancel_matcher = on_message(
    rule=Rule(is_manual_cancel_verification), priority=3, block=True
)


@manual_cancel_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    reply = await reply_fetch(event, bot)
    if not reply:
        return

    key = _resolve_verification_key_from_reply(bot, event, reply, require_pending=False)
    if not key:
        return

    task_info = _pop_verification_task(key)
    if task_info:
        _cancel_timeout_task(task_info)
        _release_reserved_group_welcome_cooldown(key[0])
    await MessageUtils.build_message("已取消此次验证.").send(reply_to=True)
    logger.info(
        f"管理员 {event.user_id} 手动取消群 {key[0]} 用户 {key[1]} 的进群验证。",
        PLUGIN_MODULE,
        target=key[0],
    )


verification_matcher = on_message(
    rule=Rule(is_in_verification), priority=4, block=False
)


@verification_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    group_id = event.group_id
    user_id = event.user_id
    key = (group_id, user_id)

    if key not in verification_tasks:
        return

    if not await _bot_has_group_admin_permission(bot, group_id):
        await _cancel_group_verifications_for_no_permission(
            bot,
            group_id,
            "Bot当前没有群管理权限，已取消进群验证",
        )
        return

    user_input = event.get_plaintext().strip()
    task_info = verification_tasks[key]
    correct_ans = task_info["answer"]

    is_correct = False
    is_valid_digit = False
    
    # 使用 try-except 判断是否为有效整数并核对答案
    try:
        user_ans = int(user_input)
        is_valid_digit = True
        if user_ans == correct_ans:
            is_correct = True
    except ValueError:
        pass

    if is_correct:
        _cancel_timeout_task(task_info)
        _pop_verification_task(key)
        success_msg = MessageUtils.build_message("验证通过~")
        if not await CommonUtils.task_is_block(bot, "group_welcome", str(group_id)):
            try:
                from zhenxun.builtin_plugins.platform.qq.group_handle.data_source import (
                    GroupManager,
                )

                if task_info.get("welcome_reserved") or _can_send_group_welcome(
                    group_id
                ):
                    _start_group_welcome_cooldown(group_id)
                    success_msg += UniMessage.text("\n") + _build_welcome_message(
                        group_id, user_id
                    )
            except Exception as e:
                logger.error("验证通过后发送欢迎消息失败", PLUGIN_MODULE, e=e)
        await success_msg.send(reply_to=True)
        logger.info(f"用户 {user_id} 在群 {group_id} 验证通过。", PLUGIN_MODULE)
    else:
        # 无论是发错数字还是发非数字，都算作一次失败尝试
        task_info["retries"] += 1
        _save_pending_verifications()
        remaining = task_info["max_retries"] - task_info["retries"]

        if remaining <= 0:
            _cancel_timeout_task(task_info)
            _pop_verification_task(key)
            await MessageUtils.build_message("验证失败次数过多，已移出群聊。").send(
                reply_to=True
            )
            try:
                await bot.set_group_kick(
                    group_id=group_id, user_id=user_id, reject_add_request=False
                )
                logger.info(
                    f"用户 {user_id} 在群 {group_id} 验证失败次数过多，已被踢出。",
                    PLUGIN_MODULE,
                )
            except Exception as e:
                _release_reserved_group_welcome_cooldown(group_id)
                logger.error(f"踢出用户 {user_id} 失败: {e}", PLUGIN_MODULE)
                await _notify_no_permission(
                    bot,
                    group_id,
                    user_id,
                    f"验证失败后踢出用户失败，可能是Bot没有群管理权限: {e}",
                )
        else:
            # 根据输入内容给出不同的提示，并附带剩余次数
            if not is_valid_digit:
                tip_msg = f"请输入纯数字答案.. (剩余尝试次数: {remaining})"
            else:
                tip_msg = f"答案错误..请重新回答. (剩余尝试次数: {remaining})"
                
            await MessageUtils.build_message(tip_msg).send(reply_to=True)
