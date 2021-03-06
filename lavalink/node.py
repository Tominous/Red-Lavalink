import asyncio
import contextlib
import json
from collections import namedtuple
from enum import Enum
from typing import Awaitable, Dict, List, Optional, cast

import websockets
from discord.backoff import ExponentialBackoff

from . import log
from .rest_api import Track


__all__ = [
    "DiscordVoiceSocketResponses",
    "LavalinkEvents",
    "TrackEndReason",
    "LavalinkOutgoingOp",
    "PlayerState",
    "Stats",
    "Node",
    "get_node",
    "join_voice",
]

_nodes = {}  # type: Dict[Node, List[int]]


class DiscordVoiceSocketResponses(Enum):
    VOICE_STATE_UPDATE = "VOICE_STATE_UPDATE"
    VOICE_SERVER_UPDATE = "VOICE_SERVER_UPDATE"


class LavalinkIncomingOp(Enum):
    EVENT = "event"
    PLAYER_UPDATE = "playerUpdate"
    STATS = "stats"


class LavalinkOutgoingOp(Enum):
    VOICE_UPDATE = "voiceUpdate"
    DESTROY = "destroy"
    PLAY = "play"
    STOP = "stop"
    PAUSE = "pause"
    SEEK = "seek"
    VOLUME = "volume"


class LavalinkEvents(Enum):
    """
    An enumeration of the Lavalink Track Events.
    """

    TRACK_END = "TrackEndEvent"
    """The track playback has ended."""

    TRACK_EXCEPTION = "TrackExceptionEvent"
    """There was an exception during track playback."""

    TRACK_STUCK = "TrackStuckEvent"
    """Track playback got stuck during playback."""

    # Custom events
    TRACK_START = "TrackStartEvent"
    """This is a custom event generated by this library that is used to
    denote the start of a track.
    """

    QUEUE_END = "QueueEndEvent"
    """This is a custom event generated by this library to denote the
    end of all tracks in the queue.
    """


class TrackEndReason(Enum):
    """
    The reasons why track playback has ended.
    """

    FINISHED = "FINISHED"
    """The track reached the end, or the track itself ended with an
    exception.
    """

    LOAD_FAILED = "LOAD_FAILED"
    """The track failed to start, throwing an exception before
    providing any audio.
    """

    STOPPED = "STOPPED"
    """The track was stopped due to the player being stopped.
    """

    REPLACED = "REPLACED"
    """The track stopped playing because a new track started playing.
    """

    CLEANUP = "CLEANUP"
    """The track was stopped because the cleanup threshold for the
    audio player was reached.
    """


PlayerState = namedtuple("PlayerState", "position time")
MemoryInfo = namedtuple("MemoryInfo", "reservable used free allocated")
CPUInfo = namedtuple("CPUInfo", "cores systemLoad lavalinkLoad")


class Stats:
    def __init__(self, memory, players, active_players, cpu, uptime):
        self.memory = MemoryInfo(**memory)
        self.players = players
        self.active_players = active_players
        self.cpu_info = CPUInfo(**cpu)
        self.uptime = uptime


class Node:

    _is_shutdown = False  # type: bool

    def __init__(
        self, _loop, event_handler, voice_ws_func, host, password, port, rest, user_id, num_shards
    ):
        """
        Represents a Lavalink node.

        Parameters
        ----------
        _loop : asyncio.BaseEventLoop
            The event loop of the bot.
        event_handler
            Function to dispatch events to.
        voice_ws_func : typing.Callable
            Function that takes one argument, guild ID, and returns a websocket.
        host : str
            Lavalink player host.
        password : str
            Password for the Lavalink player.
        port : int
            Port of the Lavalink player event websocket.
        rest : int
            Port for the Lavalink REST API.
        user_id : int
            User ID of the bot.
        num_shards : int
            Number of shards to which the bot is currently connected.
        ready : asyncio.Event
            Set when the connection is up and running, unset when not.
        """
        self.loop = _loop
        self.event_handler = event_handler
        self.voice_ws_func = voice_ws_func
        self.host = host
        self.port = port
        self.rest = rest
        self.password = password
        self.headers = self._get_connect_headers(self.password, user_id, num_shards)

        self.ready = asyncio.Event()

        self._ws = None
        self._listener_task = None

        self._queue = []
        self._players = set()

    async def connect(self, timeout=None):
        """
        Connects to the Lavalink player event websocket.

        Parameters
        ----------
        timeout : int
            Time after which to timeout on attempting to connect to the Lavalink websocket,
            ``None`` is considered never, but the underlying code may stop trying past a
            certain point.

        Raises
        ------
        asyncio.TimeoutError
            If the websocket failed to connect after the given time.
        """
        self._is_shutdown = False

        combo_uri = "ws://{}:{}".format(self.host, self.rest)
        uri = "ws://{}:{}".format(self.host, self.port)

        log.debug(
            "Lavalink WS connecting to %s or %s with headers %s", combo_uri, uri, self.headers
        )

        tasks = tuple({self._multi_try_connect(u) for u in (combo_uri, uri)})

        for task in asyncio.as_completed(tasks, timeout=timeout):
            with contextlib.suppress(Exception):
                if await cast(Awaitable[Optional[websockets.WebSocketClientProtocol]], task):
                    break
        else:
            raise asyncio.TimeoutError

        _nodes[self] = []

        log.debug("Creating Lavalink WS listener.")
        self._listener_task = self.loop.create_task(self.listener())

        for data in self._queue:
            await self.send(data)

        self.ready.set()

    async def wait_until_ready(self, timeout: Optional[float] = None):
        await asyncio.wait_for(self.ready.wait(), timeout=timeout)

    @staticmethod
    def _get_connect_headers(password, user_id, num_shards):
        return {"Authorization": password, "User-Id": user_id, "Num-Shards": num_shards}

    @property
    def lavalink_major_version(self):
        assert self._ws, "not connected"
        return self._ws.response_headers.get("Lavalink-Major-Version")

    async def _multi_try_connect(self, uri):
        backoff = ExponentialBackoff()
        attempt = 1

        while self._is_shutdown is False and (self._ws is None or not self._ws.open):
            try:
                ws = self._ws = await websockets.connect(uri, extra_headers=self.headers)
                return ws
            except OSError:
                delay = backoff.delay()
                log.debug("Failed connect attempt %s, retrying in %s", attempt, delay)
                await asyncio.sleep(delay)
                attempt += 1
            except websockets.InvalidStatusCode:
                return None

    async def listener(self):
        """
        Listener task for receiving ops from Lavalink.
        """
        while self._ws.open and self._is_shutdown is False:
            try:
                data = json.loads(await self._ws.recv())
            except websockets.ConnectionClosed:
                break

            raw_op = data.get("op")
            try:
                op = LavalinkIncomingOp(raw_op)
            except ValueError:
                log.debug("Received unknown op: %s", data)
            else:
                log.debug("Received known op: %s", data)
                self.loop.create_task(self._handle_op(op, data))

        self.ready.clear()
        log.debug("Listener exited: ws %s SHUTDOWN %s.", self._ws.open, self._is_shutdown)
        self.loop.create_task(self._reconnect())

    async def _handle_op(self, op: LavalinkIncomingOp, data):
        if op == LavalinkIncomingOp.EVENT:
            try:
                event = LavalinkEvents(data.get("type"))
            except ValueError:
                log.debug("Unknown event type: %s", data)
            else:
                self.event_handler(op, event, data)
        elif op == LavalinkIncomingOp.PLAYER_UPDATE:
            state = PlayerState(**data.get("state"))
            self.event_handler(op, state, data)
        elif op == LavalinkIncomingOp.STATS:
            stats = Stats(
                memory=data.get("memory"),
                players=data.get("players"),
                active_players=data.get("playingPlayers"),
                cpu=data.get("cpu"),
                uptime=data.get("uptime"),
            )
            self.event_handler(op, stats, data)

    async def _reconnect(self):
        self.ready.clear()

        if self._is_shutdown is True:
            log.debug("Shutting down Lavalink WS.")
            return

        log.debug("Attempting Lavalink WS reconnect.")
        try:
            await self.connect()
        except asyncio.TimeoutError:
            log.debug("Failed to reconnect, please reinitialize lavalink when ready.")
        else:
            log.debug("Reconnect successful.")

    async def disconnect(self):
        """
        Shuts down and disconnects the websocket.
        """
        self._is_shutdown = True
        self.ready.clear()

        if self._ws is not None and self._ws.open:
            await self._ws.close()

        while self._players:
            await self._players.pop().destroy()

        if _nodes.pop(self, None):
            log.debug("Shutdown Lavalink WS.")

    async def send(self, data):
        if self._ws is None or not self._ws.open:
            self._queue.append(data)
        else:
            log.debug("Sending data to Lavalink: %s", data)
            await self._ws.send(json.dumps(data))

    async def send_lavalink_voice_update(self, guild_id, session_id, event):
        await self.send(
            {
                "op": LavalinkOutgoingOp.VOICE_UPDATE.value,
                "guildId": str(guild_id),
                "sessionId": session_id,
                "event": event,
            }
        )

    async def destroy_guild(self, guild_id: int):
        await self.send({"op": LavalinkOutgoingOp.DESTROY.value, "guildId": str(guild_id)})

    # Player commands
    async def stop(self, guild_id: int):
        await self.send({"op": LavalinkOutgoingOp.STOP.value, "guildId": str(guild_id)})
        self.event_handler(
            LavalinkIncomingOp.EVENT, LavalinkEvents.QUEUE_END, {"guildId": str(guild_id)}
        )

    async def play(self, guild_id: int, track: Track):
        await self.send(
            {
                "op": LavalinkOutgoingOp.PLAY.value,
                "guildId": str(guild_id),
                "track": track.track_identifier,
            }
        )
        self.event_handler(
            LavalinkIncomingOp.EVENT,
            LavalinkEvents.TRACK_START,
            {"guildId": str(guild_id), "track": track},
        )

    async def pause(self, guild_id, paused):
        await self.send(
            {"op": LavalinkOutgoingOp.PAUSE.value, "guildId": str(guild_id), "pause": paused}
        )

    async def volume(self, guild_id: int, _volume: int):
        await self.send(
            {"op": LavalinkOutgoingOp.VOLUME.value, "guildId": str(guild_id), "volume": _volume}
        )

    async def seek(self, guild_id: int, position: int):
        await self.send(
            {"op": LavalinkOutgoingOp.SEEK.value, "guildId": str(guild_id), "position": position}
        )


def get_node(guild_id: int) -> Node:
    """
    Gets a node based on a guild ID, useful for noding separation. If the
    guild ID does not already have a node association, the least used
    node is returned. Skips over nodes that are not yet ready.

    Parameters
    ----------
    guild_id : int

    Raises
    ------
    IndexError
        If no Nodes have been instantiated yet.

    Returns
    -------
    Node
    """
    guild_count = 1e10
    least_used = None

    if not _nodes:
        raise IndexError("no Nodes have been instantiated")

    for node, guild_ids in _nodes.items():
        if not node.ready.is_set():
            continue
        elif len(guild_ids) < guild_count:
            guild_count = len(guild_ids)
            least_used = node

        if guild_id in guild_ids:
            return node

    if least_used is None:
        raise IndexError("No nodes found.")

    _nodes[least_used].append(guild_id)
    return least_used


async def join_voice(guild_id: int, channel_id: int):
    """
    Joins a voice channel by ID's.

    Parameters
    ----------
    guild_id : int
    channel_id : int
    """
    node = get_node(guild_id)
    voice_ws = node.voice_ws_func(guild_id)
    await voice_ws.voice_state(guild_id, channel_id)


async def disconnect():
    nodes = list(_nodes.keys())
    for node in nodes:
        await node.disconnect()
