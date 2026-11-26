"""Optional CLAP adapter. Both model and processor must already exist locally.

No implicit weight download or text translation is performed. English descriptions
are recommended for the published CLAP checkpoints; other languages are unvalidated.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def weights_fingerprint(root: Path) -> str:
    """Model id from the weight files alone (no torch import), identical to LocalCLAP.model_id."""
    digest = hashlib.sha256()
    for path in sorted([*Path(root).glob("*.safetensors"), *Path(root).glob("pytorch_model*.bin")]):
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return "clap:" + digest.hexdigest()


class LocalCLAP:
    def __init__(self, directory: str | None = None):
        from .runtime import model_directory

        root = Path(directory) if directory else model_directory()
        if not root.is_dir() or not (root / "config.json").is_file():
            raise ValueError("Set AGENT_BRIDGE_CLAP_MODEL to an existing local Hugging Face CLAP model directory")
        try:
            import torch
            from transformers import ClapModel, ClapProcessor
        except ImportError:
            raise ValueError('Semantic search needs optional dependencies: uv pip install -e ".[audio,semantic]"') from None
        torch.set_num_threads(2)
        # Disable network for every loader, even on a computer with cached credentials.
        self.processor = ClapProcessor.from_pretrained(str(root), local_files_only=True)
        # Published weights are a state dict; PyTorch's restricted weights-only loader is mandatory.
        self.model = ClapModel.from_pretrained(str(root), local_files_only=True, weights_only=True,
                                              use_safetensors=any(root.glob("*.safetensors"))).to("cpu").eval()
        self.torch = torch
        self.model_id = weights_fingerprint(root)

    def audio(self, path: str) -> list[float]:
        import math

        import soundfile as sf
        from scipy.signal import resample_poly

        from .features import check_decodable

        check_decodable(path)
        with sf.SoundFile(path) as stream:
            sr = stream.samplerate
            data = stream.read(min(stream.frames, sr * 10), dtype="float32", always_2d=True).mean(axis=1)
        divisor = math.gcd(sr, 48000)
        data = resample_poly(data, 48000 // divisor, sr // divisor)
        return self.audio_array(data)

    def audio_array(self, data) -> list[float]:
        inputs = self.processor(audios=data, sampling_rate=48000, return_tensors="pt")
        with self.torch.inference_mode():
            vector = self.model.get_audio_features(**inputs)
        return vector[0].cpu().numpy().tolist()

    def text(self, description: str) -> list[float]:
        return self.texts([description])[0]

    def texts(self, descriptions: list[str]) -> list[list[float]]:
        inputs = self.processor(text=descriptions, return_tensors="pt", padding=True)
        with self.torch.inference_mode():
            vector = self.model.get_text_features(**inputs)
        return vector.cpu().numpy().tolist()
