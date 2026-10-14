"""Use cloud credits immediately; route excess/failed requests to the local GPU."""

import asyncio

from nastya_worker.providers.http import InferenceError


class HybridSpeechRecognizer:
    def __init__(self, remote, local, timeout_seconds=1.2):
        self.remote, self.local = remote, local
        self.timeout_seconds = timeout_seconds

    async def transcribe(self, wav, language):
        try:
            return await asyncio.wait_for(
                self.remote.transcribe(wav, language, wait_for_slot=False),
                timeout=self.timeout_seconds,
            )
        except (InferenceError, TimeoutError):
            return await self.local.transcribe(wav, language)
