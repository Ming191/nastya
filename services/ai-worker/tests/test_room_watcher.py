import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nastya_worker.config import Settings
from nastya_worker.room_watcher import eligible_rooms, watch_rooms

ROOM = "nastya_" + "1" * 32
OTHER = "nastya_" + "2" * 32


def test_room_discovery_filters_non_nastya_empty_and_external_workers():
    async def go():
        service = SimpleNamespace(
            list_rooms=AsyncMock(
                return_value=SimpleNamespace(
                    rooms=[
                        SimpleNamespace(name="unrelated", num_participants=2),
                        SimpleNamespace(name=ROOM, num_participants=2),
                        SimpleNamespace(name=OTHER, num_participants=1),
                        SimpleNamespace(name="nastya_" + "3" * 32, num_participants=0),
                    ]
                )
            ),
            list_participants=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        participants=[
                            SimpleNamespace(identity="human:owner"),
                            SimpleNamespace(identity="interpreter"),
                        ]
                    ),
                    SimpleNamespace(participants=[SimpleNamespace(identity="interpreter")]),
                ]
            ),
        )
        api = SimpleNamespace(
            ListRoomsRequest=SimpleNamespace, ListParticipantsRequest=SimpleNamespace
        )
        assert await eligible_rooms(service, api) == []
        service.list_participants.side_effect = [
            SimpleNamespace(
                participants=[
                    SimpleNamespace(identity="human:guest"),
                    SimpleNamespace(identity="interpreter"),
                ]
            ),
            SimpleNamespace(participants=[SimpleNamespace(identity="interpreter")]),
        ]
        assert await eligible_rooms(service, api, ROOM) == [ROOM]

    asyncio.run(go())


def test_watcher_starts_one_room_and_cancels_when_empty():
    async def go():
        from livekit import api

        client = AsyncMock()
        client.__aenter__.return_value = client
        started = []
        stopped = []

        async def serve(room, settings):
            started.append(room)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(room)

        original_sleep = asyncio.sleep
        sleeps = 0

        async def sleep(_):
            nonlocal sleeps
            sleeps += 1
            await original_sleep(0)
            if sleeps == 2:
                raise asyncio.CancelledError

        keys = dict(LIVEKIT_URL="wss://test", LIVEKIT_API_KEY="test", LIVEKIT_API_SECRET="test")
        with (
            patch("nastya_worker.room_watcher.room_settings", return_value=keys),
            patch.object(api, "LiveKitAPI", return_value=client),
            patch(
                "nastya_worker.room_watcher.eligible_rooms",
                new=AsyncMock(side_effect=[[ROOM, OTHER], []]),
            ),
            patch("nastya_worker.room_watcher.serve_room", new=serve),
            patch("nastya_worker.room_watcher.asyncio.sleep", new=sleep),
        ):
            try:
                await watch_rooms(Settings(stt_url="https://test/stt", mt_url="https://test/mt"))
            except asyncio.CancelledError:
                pass
        assert started == [ROOM]
        assert stopped == [ROOM]

    asyncio.run(go())
