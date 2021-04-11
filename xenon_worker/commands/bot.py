from ..connection.rabbit import RabbitClient
from ..connection.entities import Message
from .command import CommandTable
from .context import Context
from .module import Listener
from .formatter import Formatter, FormatRaise
import traceback
from .errors import *
import sys
import shlex
import asyncio
import random


class RabbitBot(RabbitClient, CommandTable):
    def __init__(self, prefix, *args, **kwargs):
        RabbitClient.__init__(self, *args, **kwargs)
        CommandTable.__init__(self)
        self.prefix = prefix
        self.static_listeners = {}
        self.modules = []
        self.f = Formatter()

    def f_send(self, channel, *args, **kwargs):
        return self.send_message(channel, **self.f.format(*args, **kwargs))

    def _process_listeners(self, event, *args, **kwargs):
        s_listeners = self.static_listeners.get(event.name, [])
        for listener in s_listeners:
            self.loop.create_task(listener.execute(event.shard_id, *args, **kwargs))

        super()._process_listeners(event, *args, **kwargs)

    async def process_commands(self, shard_id, msg):
        try:
            parts = shlex.split(msg.content)
        except ValueError:
            parts = msg.content.split(" ")

        try:
            parts, cmd = self.find_command(parts)
        except CommandNotFound:
            return

        await self.redis.hincrby("commands", cmd.full_name, 1)

        bucket = msg.guild_id or msg.author.id

        is_blacklisted = await self.redis.exists(f"blacklist:{bucket}")
        if is_blacklisted or msg.author.created_at > (datetime.utcnow() - timedelta(days=1)):
            await self.redis.incr("commands:blocked")
            await self.redis.setex(f"blacklist:{bucket}", random.randint(60 * 15, 60 * 60), 1)
            return

        cmd_count = int(await self.redis.get(f"commands:{bucket}") or 0)
        if cmd_count > 5:
            # temp silent blacklist
            await self.redis.setex(f"blacklist:{bucket}", random.randint(60 * 15, 60 * 60), 1)
            return

        else:
            await self.redis.setex(f"commands:{bucket}", 2, cmd_count + 1)

        ctx = Context(self, shard_id, msg)
        try:
            await cmd.execute(ctx, parts)
        except Exception as e:
            self.dispatch("command_error", cmd, ctx, e)

    async def invoke(self, ctx, cmd):
        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = cmd.split(" ")

        try:
            parts, cmd = self.find_command(parts)
        except CommandNotFound:
            return

        await cmd.execute(ctx, parts)

    async def on_command_error(self, _, cmd, ctx, e):
        if isinstance(e, asyncio.CancelledError):
            return

        if isinstance(e, FormatRaise):
            await ctx.f_send(*e.args, **e.kwargs, f=e.f)
            return

        elif isinstance(e, CommandNotFound):
            return

        elif isinstance(e, NotEnoughArguments):
            await ctx.f_send(
                f"The command `{cmd.full_name}` is **missing the `{e.parameter.name}` argument**.\n"
                f"Use `{self.prefix}help {cmd.full_name}` to get more information.",
                f=self.f.ERROR
            )

        elif isinstance(e, ConverterFailed):
            name = e.parameter.converter.__name__
            name = name.replace("Converter", "")

            common_types = {
                "int": "number",
                "float": "decimal number",
                "str": "text"
            }
            name = common_types.get(name, name)

            await ctx.f_send(
                f"The **value `{e.value}`** passed to `{e.parameter.name}` is **not a valid `{name}`**",
                f=self.f.ERROR
            )

        elif isinstance(e, MissingPermissions):
            await ctx.f_send(
                f"You are **missing** the following **permissions**: `{', '.join(e.missing)}`.",
                f=self.f.ERROR
            )

        elif isinstance(e, BotMissingPermissions):
            await ctx.f_send(
                f"The bot is **missing** the following **permissions**: `{', '.join(e.missing)}`.",
                f=self.f.ERROR
            )

        elif isinstance(e, NotOwner):
            await ctx.f_send(
                "This command can **only** be used by the **server owner**.",
                f=self.f.ERROR
            )

        elif isinstance(e, NotBotOwner):
            await ctx.f_send(
                "This command can **only** be used by the **bot owner**.",
                f=self.f.ERROR
            )

        elif isinstance(e, NotAGuildChannel):
            await ctx.f_send(
                "This command can **only** be used **inside a guild**.",
                f=self.f.ERROR
            )

        elif isinstance(e, NotADMChannel):
            await ctx.f_send(
                "This command can **only** be used in **direct messages**.",
                f=self.f.ERROR
            )

        elif isinstance(e, BotInMaintenance):
            await ctx.f_send(
                "The bot is currently in **maintenance**. This command can not be used during maintenance,"
                " please be patient and **try again in a few minutes**.",
                f=self.f.ERROR
            )

        elif isinstance(e, CommandOnCooldown):
            if e.warned:
                return

            await ctx.f_send(
                f"This **command** is currently on **cooldown**.\n"
                f"You have to **wait `{e.remaining}` seconds** until you can use it again.",
                f=self.f.ERROR
            )

        else:
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            print(tb, file=sys.stderr)
            await ctx.f_send(f"```py\n{e.__class__.__name__}:\n{str(e)}\n```", f=self.f.ERROR)
            # try:
            #     await ctx.f_send(f"```py\n{tb}```", f=self.f.ERROR)
            # except:
            #     pass

    async def on_command(self, shard_id, data):
        msg = Message(data)
        await self.process_commands(shard_id, msg)

    def add_listener(self, listener):
        if listener.name not in self.static_listeners.keys():
            self.static_listeners[listener.name] = []

        listeners = self.static_listeners[listener.name]
        listeners.append(listener)

    def listener(self, *args, **kwargs):
        def _predicate(callback):
            listener = Listener(callback, *args, **kwargs)
            self.add_listener(listener)
            return listener

        return _predicate

    def add_module(self, module):
        self.modules.append(module)
        for cmd in module.commands:
            cmd.fill_module(module)
            self.add_command(cmd)

        for listener in module.listeners:
            listener.module = module
            self.add_listener(listener)

        for task in module.tasks:
            task.module = module
            self.schedule(task.construct())

    def schedule(self, coro):
        return self.loop.create_task(coro)

    async def start(self, token, shared_queue, *shared_subs):
        subscriptions = set(shared_subs)
        subscriptions.add("command")

        await super().start(token, shared_queue, *subscriptions)
        self.dispatch("load")

    async def close(self):
        await super().close()
