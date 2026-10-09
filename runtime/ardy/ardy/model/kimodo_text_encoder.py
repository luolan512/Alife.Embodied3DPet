# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ARDY text encoder backed by the existing Kimodo LLM2Vec GGUF encoder."""

from __future__ import annotations

import os
import struct
import subprocess
import threading
import uuid
from pathlib import Path
from typing import BinaryIO

import numpy as np
import torch

_EMBEDDING_WIDTH = 4096
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_KIMODO_ROOT = _WORKSPACE_ROOT / "kimodo.cpp"


def _path_from_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


class KimodoTextEncoder:
    """Call Kimodo's native LLM2Vec GGUF encoder and return ARDY embeddings.

    The child process is started lazily on the first prompt so constructing
    this wrapper does not load the 4.39 GB text model. Once started, the same
    process is reused for all prompts.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        encoder_path: str | Path | None = None,
        backend: str = "cpu",
        threads: int = 8,
        layer_chunk: int = 2,
    ) -> None:
        self.model_path = Path(model_path or os.environ.get("KIMODO_TEXT_BUNDLE") or _DEFAULT_KIMODO_ROOT / "Llama-3-Kimodo-Q4_K.gguf")
        default_encoder = _DEFAULT_KIMODO_ROOT / "build" / "cpu" / "kmd-encode-server.exe"
        self.encoder_path = Path(encoder_path or os.environ.get("KIMODO_ENCODE_SERVER") or default_encoder)
        self.backend = backend
        self.threads = int(threads)
        self.layer_chunk = int(layer_chunk)
        self.llm_dim = _EMBEDDING_WIDTH
        self.device = "cpu"
        self.dtype = torch.float32
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self.requested_mode = 'cpu'
        self.active_backend = None
        self.fallback_reason = None
        self._cpu_encoder_path = self.encoder_path
        self._gpu_encoder_path = None
        self._requested_chunk = self.layer_chunk
        self._cancel_path = _WORKSPACE_ROOT / "character-agent" / ".run" / ("encoder-cancel-" + uuid.uuid4().hex + ".flag")

        if not self.model_path.is_file():
            raise FileNotFoundError(f"Kimodo text model not found: {self.model_path}")
        if not self.encoder_path.is_file():
            raise FileNotFoundError(f"Kimodo encoder executable not found: {self.encoder_path}")

    def _start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return

        env = os.environ.copy()
        env["KIMODO_CANCEL_FILE"] = str(self._cancel_path)
        # CMake places the GGML runtime DLLs in bin/ beside the encoder.
        if os.name == "nt":
            runtime_dirs = [self.encoder_path.parent, self.encoder_path.parent / "bin"]
            env["PATH"] = os.pathsep.join(str(path) for path in runtime_dirs) + os.pathsep + env.get("PATH", "")
        env["KIMODO_BACKEND"] = self.backend
        env.setdefault("KIMODO_THREADS", str(self.threads))
        env["KIMODO_TEXT_LAYER_CHUNK"] = str(self.layer_chunk)
        if self.requested_mode == 'vulkan-resident':
            env.pop('KIMODO_TEXT_RESIDENT_LIMIT_MIB', None)

        self._process = subprocess.Popen(
            [str(self.encoder_path), str(self.model_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        assert self._process.stdout is not None
        ready = self._read_exact(self._process.stdout, 4)
        if ready != struct.pack("<I", 1):
            process = self._process
            self.close()
            stderr = ""
            if process is not None and process.stderr is not None:
                stderr = process.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Kimodo text encoder failed to start (exit={process.returncode}): {stderr.strip()}")
        self.active_backend=self.backend

    def select_backend(self,mode,gpu_encoder_path,layer_chunk=2):
        if mode not in ('cpu','vulkan-stream','vulkan-resident') or type(layer_chunk) is not int or not 1<=layer_chunk<=8:
            raise ValueError('Invalid text encoder mode or chunk size')
        with self._lock:
            self.close();self.requested_mode=mode;self._requested_chunk=layer_chunk
            self._gpu_encoder_path=Path(gpu_encoder_path)
            self.backend='cpu' if mode=='cpu' else 'vulkan'
            self.layer_chunk=32 if mode=='vulkan-resident' else layer_chunk
            self.encoder_path=self._cpu_encoder_path if mode=='cpu' else self._gpu_encoder_path
            self.active_backend=None;self.fallback_reason=None
            if mode!='cpu' and not self.encoder_path.is_file():self._fallback_cpu('Vulkan executable is not installed')

    def _fallback_cpu(self,reason):
        self.close();self.backend='cpu';self.encoder_path=self._cpu_encoder_path;self.layer_chunk=self._requested_chunk
        self.fallback_reason=reason;self.active_backend=None

    def backend_status(self):
        return {'requested_mode':self.requested_mode,'active_backend':self.active_backend,
                'layer_chunk':self.layer_chunk,'fallback_reason':self.fallback_reason,
                'process_started':self._process is not None and self._process.poll() is None}

    def _check_resident_memory(self):
        if self._process is not None and self._process.poll() is None:return
        if self.backend!='vulkan' or self.layer_chunk!=32 or not torch.cuda.is_available():return
        # NVIDIA's free memory is shared with Vulkan; reserve graph/workspace room.
        free,_=torch.cuda.mem_get_info()
        needed=self.model_path.stat().st_size+768*1024**2
        if free<needed:
            self.layer_chunk=self._requested_chunk
            self.fallback_reason='Insufficient free VRAM for residency; using Vulkan streaming'

    @staticmethod
    def _read_exact(stream: BinaryIO, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _encode_one(self, prompt: str) -> np.ndarray:
        assert self._process is not None and self._process.stdin is not None
        assert self._process.stdout is not None

        payload = prompt.encode("utf-8")
        if not payload:
            raise ValueError("prompt must not be empty")
        self._process.stdin.write(struct.pack("<I", len(payload)))
        self._process.stdin.write(payload)
        self._process.stdin.flush()

        status_bytes = self._read_exact(self._process.stdout, 4)
        if len(status_bytes) != 4:
            raise RuntimeError("Kimodo text encoder closed unexpectedly")
        status = struct.unpack("<I", status_bytes)[0]
        if status == 0:
            error_size_bytes = self._read_exact(self._process.stdout, 4)
            if len(error_size_bytes) != 4:
                raise RuntimeError("Kimodo text encoder returned a malformed error")
            error_size = struct.unpack("<I", error_size_bytes)[0]
            error = self._read_exact(self._process.stdout, error_size).decode("utf-8", errors="replace")
            raise RuntimeError(f"Kimodo text encoding failed: {error}")

        size_bytes = self._read_exact(self._process.stdout, 4)
        if len(size_bytes) != 4:
            raise RuntimeError("Kimodo text encoder returned a truncated embedding size")
        count = struct.unpack("<I", size_bytes)[0]
        if count != _EMBEDDING_WIDTH:
            raise RuntimeError(f"Kimodo text encoder returned {count} values; expected {_EMBEDDING_WIDTH}")

        raw = self._read_exact(self._process.stdout, count * 4)
        if len(raw) != count * 4:
            raise RuntimeError("Kimodo text encoder returned a truncated embedding")
        return np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=True)

    def to(self, device=None, dtype=None):
        if device is not None:
            self.device = str(device)
        if dtype is not None:
            self.dtype = dtype
        return self

    def eval(self):
        return self

    def get_device(self):
        return torch.device(self.device)

    def cancel_current(self) -> None:
        """Signal the owned encoder; it stops between bounded layer graphs."""
        self._cancel_path.parent.mkdir(parents=True, exist_ok=True)
        self._cancel_path.touch()

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __call__(self, texts: list[str] | str):
        is_string = isinstance(texts, str)
        prompts = [texts] if is_string else list(texts)
        if not prompts:
            raise ValueError("texts must contain at least one prompt")

        with self._lock:
            self._cancel_path.unlink(missing_ok=True)
            self._check_resident_memory()
            try:
                self._start()
                embeddings = [self._encode_one(prompt) for prompt in prompts]
            except (RuntimeError, OSError) as exc:
                if self.backend!='vulkan' or 'cancelled' in str(exc).lower():raise
                self._fallback_cpu(str(exc)[:500])
                self._start()
                embeddings = [self._encode_one(prompt) for prompt in prompts]

        if is_string:
            tensor = torch.from_numpy(embeddings[0].reshape(1, _EMBEDDING_WIDTH))
            return tensor.to(device=self.device, dtype=self.dtype), 1

        tensor = torch.from_numpy(np.stack(embeddings)[:, np.newaxis, :])
        return tensor.to(device=self.device, dtype=self.dtype), [1] * len(prompts)
