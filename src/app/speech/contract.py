"""Bounded speech contract shared by host and future sidecars.

Timeouts, process ownership and logs are delegated to the engine launcher.
No automatic replay: a host timeout/cancellation must stop its owned process.
"""
from pathlib import Path
from typing import Protocol
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

MAX_AUDIO = 24 * 1024 * 1024


class SynthesisRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=20000)
    voice_id: str = Field(default='', max_length=200)
    lang: str = Field(default='', max_length=20)
    ref_audio: str = Field(default='', max_length=2000)
    ref_text: str = Field(default='', max_length=2000)
    speed: float | None = Field(default=None, ge=0.5, le=2)
    stream: bool = False


class SpeechEngine(Protocol):
    id: str
    def health(self) -> dict: ...
    async def synthesize(self, request: SynthesisRequest) -> bytes: ...
    async def transcribe(self, path: Path) -> dict: ...


def check_wav(data):
    if len(data) > MAX_AUDIO:
        raise HTTPException(502, '语音结果超过 24 MiB，请缩短文字')
    if not data.startswith(b'RIFF') or data[8:12] != b'WAVE':
        raise HTTPException(502, '本地语音服务未返回有效 WAV 音频')
    return data
