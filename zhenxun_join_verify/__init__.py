import asyncio
import os
import random
from typing import Any

from nonebot import on_message, on_notice
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
)
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot_plugin_alconna import At, UniMessage

from zhenxun.configs.config import Config
from zhenxun.configs.utils import PluginExtraData, RegisterConfig, Task
from zhenxun.services.log import logger
from zhenxun.utils.common_utils import CommonUtils
from zhenxun.utils.message import MessageUtils

__plugin_meta__ = PluginMetadata(
    name="进群验证",
    description="新人进群需在规定时间内回答验证题目，否则将被踢出群聊（被动插件，默认关闭）",
    usage="""
    新人进群时，若群内开启了此被动任务，机器人会自动@新人发送一道验证题目。
    新人直接回复纯数字答案即可。答错指定次数或超时未答，将被自动移出群聊。
    
    开启/关闭指令（需群管/群主）：
    开启被动 进群验证
    关闭被动 进群验证
    """.strip(),
    extra=PluginExtraData(
        author="AIGC_Hychan2333",
        version="1.2",
        tasks=[
            Task(
                module="join_verify",
                name="进群验证",
                create_status=False,
                default_status=False,
            )
        ],
        configs=[
            RegisterConfig(
                module="join_verify",
                key="timeout",
                value=600,
                type=int,
                help="验证超时时间(秒)，默认600秒(10分钟)",
            ),
            RegisterConfig(
                module="join_verify",
                key="max_retries",
                value=3,
                type=int,
                help="允许答错的最大次数",
            ),
        ],
    ).to_dict(),
)

verification_tasks: dict[tuple[int, int], dict[str, Any]] = {}


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
        return True
    except Exception as e:
        logger.error("预占入群欢迎冷却失败", "join_verify", e=e)
        return False


def _start_group_welcome_cooldown(group_id: int | str) -> None:
    from zhenxun.builtin_plugins.platform.qq.group_handle.data_source import (
        GroupManager,
    )

    for key in _welcome_cooldown_keys(group_id):
        GroupManager._flmt.start_cd(key)


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
        del verification_tasks[key]
        try:
            await bot.set_group_kick(
                group_id=group_id, user_id=user_id, reject_add_request=False
            )
            await MessageUtils.build_message(
                f"用户 [{user_id}] 验证超时，已被移出群聊."
            ).send()
            logger.info(
                f"用户 {user_id} 在群 {group_id} 验证超时，已被自动踢出。",
                "join_verify",
            )
        except Exception as e:
            logger.error(
                f"踢出用户 {user_id} 失败 (可能已退群或权限不足): {e}", "join_verify"
            )


increase_notice = on_notice(priority=0, block=False)


@increase_notice.handle()
async def _(bot: Bot, event: GroupIncreaseNoticeEvent):
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
    is_blocked = await CommonUtils.task_is_block(bot, "join_verify", str(group_id))
    logger.info(f"被动任务状态: is_blocked={is_blocked}")
    if is_blocked:
        logger.info("被动任务未开启，跳过")
        return

    problem, answer = generate_math_problem()
    base_config = Config.get("join_verify")
    timeout = base_config.get("timeout")
    max_retries = base_config.get("max_retries")

    welcome_reserved = False
    if not await CommonUtils.task_is_block(bot, "group_welcome", str(group_id)):
        welcome_reserved = _reserve_group_welcome_cooldown(group_id, timeout + 30)

    verification_tasks[key] = {
        "answer": answer,
        "retries": 0,
        "max_retries": max_retries,
        "welcome_reserved": welcome_reserved,
    }

    task = asyncio.create_task(timeout_kick(bot, group_id, user_id, timeout))
    verification_tasks[key]["task"] = task

    msg = UniMessage().at(str(user_id)) + (
        f" 为了防止坏人，请在{timeout // 60}分钟内回答以下题目：\n"
        f"[ {problem} ]\n"
        f"请直接回复纯数字答案。答错{max_retries}次或超时将被移出群聊。"
    )

    logger.info(f"准备发送验证消息给群 {group_id} 的用户 {user_id}")
    await msg.send()
    logger.info("验证消息发送完成")


def is_in_verification(event: GroupMessageEvent) -> bool:
    return (event.group_id, event.user_id) in verification_tasks


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
        task_info["task"].cancel()
        del verification_tasks[key]
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
                logger.error("验证通过后发送欢迎消息失败", "join_verify", e=e)
        await success_msg.send(reply_to=True)
        logger.info(f"用户 {user_id} 在群 {group_id} 验证通过。", "join_verify")
    else:
        # 无论是发错数字还是发非数字，都算作一次失败尝试
        task_info["retries"] += 1
        remaining = task_info["max_retries"] - task_info["retries"]

        if remaining <= 0:
            task_info["task"].cancel()
            del verification_tasks[key]
            await MessageUtils.build_message("验证失败次数过多，已移出群聊。").send(
                reply_to=True
            )
            try:
                await bot.set_group_kick(
                    group_id=group_id, user_id=user_id, reject_add_request=False
                )
                logger.info(
                    f"用户 {user_id} 在群 {group_id} 验证失败次数过多，已被踢出。",
                    "join_verify",
                )
            except Exception as e:
                logger.error(f"踢出用户 {user_id} 失败: {e}", "join_verify")
        else:
            # 根据输入内容给出不同的提示，并附带剩余次数
            if not is_valid_digit:
                tip_msg = f"请输入纯数字答案.. (剩余尝试次数: {remaining})"
            else:
                tip_msg = f"答案错误..请重新回答. (剩余尝试次数: {remaining})"
                
            await MessageUtils.build_message(tip_msg).send(reply_to=True)
