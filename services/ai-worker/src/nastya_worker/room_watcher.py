"""Local supervisor for one active Nastya room at a time."""

import asyncio
import logging
import time

from nastya_worker.config import Settings
from nastya_worker.livekit_ingest import ROOM_ID, room_settings, serve_room
from nastya_worker.speaker_pipeline import HUMANS

LOGGER = logging.getLogger(__name__)


async def eligible_rooms(room_service, api, active_room: str | None = None) -> list[str]:
    response = await room_service.list_rooms(api.ListRoomsRequest())
    candidates = sorted(
        room.name
        for room in response.rooms
        if ROOM_ID.fullmatch(room.name) and room.num_participants > 0
    )
    eligible = []
    for name in candidates[:32]:
        participants = await room_service.list_participants(api.ListParticipantsRequest(room=name))
        identities = {participant.identity for participant in participants.participants}
        if identities & HUMANS and ("interpreter" not in identities or name == active_room):
            eligible.append(name)
    return eligible


async def watch_rooms(settings: Settings) -> None:
    """Poll registered rooms; release the worker when all human participants leave."""
    if not settings.api_ready:
        raise ValueError("NASTYA_STT_URL and NASTYA_MT_URL are required for translated captions")
    keys = room_settings("nastya_" + "0" * 32)
    from livekit import api

    task = None
    active_room = None
    last_success = time.monotonic()
    async with api.LiveKitAPI(
        keys["LIVEKIT_URL"], keys["LIVEKIT_API_KEY"], keys["LIVEKIT_API_SECRET"]
    ) as client:
        LOGGER.info("Room watcher started; capacity: one active call")
        try:
            while True:
                try:
                    rooms = await eligible_rooms(client.room, api, active_room)
                    last_success = time.monotonic()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # No provider bodies, credentials or participant details in logs.
                    LOGGER.warning("Room discovery unavailable; retrying")
                    rooms = None
                expired = time.monotonic() - last_success > 30
                if task is not None and (
                    task.done() or expired or (rooms is not None and active_room not in rooms)
                ):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    task, active_room = None, None
                if task is None and rooms:
                    active_room = rooms[0]
                    task = asyncio.create_task(serve_room(active_room, settings))
                await asyncio.sleep(5)
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
