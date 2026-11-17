"""Kokoro-82M text-to-speech worker running entirely on Modal."""

import io
import os
import wave

import modal
from studio_config import TTS_APP, NAMESPACE


APP_NAME = TTS_APP
MODEL_NAME = "hexgrad/Kokoro-82M"
CACHE_DIR = "/cache"
SAMPLE_RATE = 24000

app = modal.App(APP_NAME)
cache_volume = modal.Volume.from_name(f"{NAMESPACE}-tts-cache", create_if_missing=True)

runtime = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("espeak-ng")
    .pip_install("kokoro==0.9.4")
    .env(
        {
            "HF_HOME": CACHE_DIR,
            "HF_XET_HIGH_PERFORMANCE": "1",
        }
    )
)


@app.cls(
    image=runtime,
    gpu="T4",
    volumes={CACHE_DIR: cache_volume},
    scaledown_window=300,
    timeout=600,
)
class KokoroTTSWorker:
    @modal.enter()
    def setup(self):
        from kokoro import KPipeline

        self.pipeline = KPipeline(
            lang_code="a",
            repo_id=MODEL_NAME,
            device="cuda",
        )

    @modal.method()
    def generate(
        self,
        text: str,
        voice: str = "af_heart",
        speed: float = 1.0,
    ) -> bytes:
        import numpy as np

        if not text or not text.strip():
            raise ValueError("TTS text must not be empty.")
        if speed <= 0:
            raise ValueError("TTS speed must be greater than zero.")

        audio_chunks = []
        for result in self.pipeline(
            text.strip(),
            voice=voice,
            speed=speed,
            split_pattern=r"\n+",
        ):
            audio = result.audio
            if audio is not None:
                audio_chunks.append(audio.detach().cpu().numpy())

        if not audio_chunks:
            raise RuntimeError("Kokoro returned no audio samples.")

        audio = np.concatenate(audio_chunks)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)

        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(pcm.tobytes())
        return output.getvalue()


@app.local_entrypoint()
def test_tts(
    text: str = "Humanity has always looked beyond the shoreline, wondering what waits beneath the dark water.",
    voice: str = "af_heart",
    speed: float = 1.0,
    output: str = "test_kokoro.wav",
):
    audio_bytes = KokoroTTSWorker().generate.remote(text, voice, speed)
    output_dir = os.path.dirname(output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output, "wb") as handle:
        handle.write(audio_bytes)
    print(f"Saved {len(audio_bytes)} bytes to {output}")
