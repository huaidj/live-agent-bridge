"""ACE-Step (local XL Base service) client - Python standard library only.

Contract (HuaiDJ-Music-Agent deployment):
* loopback HTTP at ACESTEP_URL (default http://127.0.0.1:18011), GET /ready
* POST /release_task {task_type: "lego", src_audio_path, ...} -> data.task_id
* POST /query_result {task_id_list: [...]} -> status 0 running / 1 done / 2 failed,
  result = JSON string list of {file: "/v1/audio?path=..."}; GET that URL to download
* the source audio must already be cropped and must live inside the service's own
  TEMP (PROCESS_TEMP/inputs) - we stage a hash-verified copy, never weaken that guard
* never silently resubmit after an uncertain timeout: every task gets a receipt and
  can be resumed by task_id
"""

from __future__ import annotations

import array
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

BASE = os.environ.get("ACESTEP_URL", "http://127.0.0.1:18011").rstrip("/")
MODEL = os.environ.get("ACESTEP_MODEL", "acestep-v15-xl-base")
PROJECT = Path(os.environ.get("ACESTEP_PROJECT", str(Path(__file__).resolve().parents[3] / "HuaiDJ-Music-Agent")))
_SIBLING_TEMP = PROJECT.parent.parent / "Temp" / "huaidj-music-agent"
_DEFAULT_PROCESS_TEMP = _SIBLING_TEMP if _SIBLING_TEMP.is_dir() else Path(tempfile.gettempdir()) / "huaidj-music-agent"
PROCESS_TEMP = Path(os.environ.get("ACESTEP_PROCESS_TEMP", str(_DEFAULT_PROCESS_TEMP)))
OUTPUT_DIR = Path(os.environ.get(
    "ACESTEP_OUTPUT_DIR", str(Path.home() / "Documents" / "Ableton" / "User Library" / "Samples" / "ACE-Step")))

_HTTP = build_opener(ProxyHandler({}))  # loopback only: never route through a system proxy


class AceStepError(Exception):
    pass


# --------------------------------------------------------------------------- HTTP

def _request(path: str, data: dict | None = None, timeout: float = 30.0) -> dict:
    body = None if data is None else json.dumps(data).encode("utf-8")
    req = Request(BASE + path, body, {"Content-Type": "application/json"})
    try:
        with _HTTP.open(req, timeout=timeout) as resp:
            return json.load(resp)
    except HTTPError as e:
        raise AceStepError(f"ACE-Step HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}") from None
    except URLError as e:
        raise AceStepError(f"ACE-Step service not reachable at {BASE} ({e.reason}); start it with "
                           f"acestep_service action=start") from None
    except OSError as e:
        # A service that shuts down after accept can reset the connection before
        # urllib has a chance to wrap it as URLError.
        raise AceStepError(f"ACE-Step connection lost at {BASE} ({e})") from None


def ready() -> dict:
    try:
        r = _request("/ready", timeout=3)
    except AceStepError as e:
        return {"ready": False, "reachable": False, "error": str(e), "url": BASE}
    r["reachable"] = True
    r["url"] = BASE
    r["model_ok"] = r.get("model") == MODEL
    return r


def service(action: str) -> dict:
    """Run the deployment's own start/stop scripts (they guard against duplicates)."""
    if action == "start":
        st = ready()
        if st.get("ready") and st.get("model_ok"):
            return {"action": action, "skipped": "service already running and ready", "status": st}
    script = PROJECT / "scripts" / ("Start-Model.ps1" if action == "start" else "Stop-Model.ps1")
    if not script.is_file():
        raise AceStepError(f"script not found: {script}")
    # The launcher and its Git preflight must not inherit MCP stdin. Use a file
    # for output because a detached service child can keep a pipe open after the
    # launcher exits. Waiting for that pipe's EOF hangs an otherwise ready start.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"}
    with tempfile.TemporaryFile() as log:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(script)], cwd=str(PROJECT), stdin=subprocess.DEVNULL,
                              stdout=log, stderr=subprocess.STDOUT, env=env, timeout=900,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        size = log.seek(0, os.SEEK_END)
        log.seek(max(0, size - 1500))
        output = log.read().decode("utf-8", errors="replace")
    out = {"action": action, "exit_code": proc.returncode,
           "output": output, "status": ready()}
    return out


# --------------------------------------------------------------------------- WAV (RIFF) helpers

def _wav_layout(path: Path) -> dict:
    with path.open("rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise AceStepError(f"not a RIFF/WAVE file: {path}")
        fmt = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                raw = f.read(size)
                fmt_tag, ch, rate, _brate, align, bits = struct.unpack("<HHIIHH", raw[:16])
                if fmt_tag == 0xFFFE and len(raw) >= 26:  # WAVE_FORMAT_EXTENSIBLE: real tag in subformat
                    fmt_tag = struct.unpack("<H", raw[24:26])[0]
                fmt = {"raw": raw, "tag": fmt_tag, "channels": ch, "rate": rate, "align": align, "bits": bits}
            elif cid == b"data":
                if fmt is None:
                    raise AceStepError("WAV data chunk before fmt chunk")
                data_size = size if size != 0xFFFFFFFF else path.stat().st_size - f.tell()
                return dict(fmt, data_offset=f.tell(), data_size=data_size,
                            seconds=data_size / float(fmt["align"] * fmt["rate"]))
            else:
                f.seek(size + (size & 1), 1)
    raise AceStepError(f"no data chunk in {path}")


def _write_wav(dest: Path, fmt_raw: bytes, payload: bytes) -> None:
    with dest.open("xb") as f:
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_raw)) + fmt_raw + (b"\0" if len(fmt_raw) & 1 else b"")
        data_chunk_hdr = b"data" + struct.pack("<I", len(payload))
        f.write(b"RIFF" + struct.pack("<I", 4 + len(fmt_chunk) + 8 + len(payload) + (len(payload) & 1)) + b"WAVE")
        f.write(fmt_chunk + data_chunk_hdr + payload + (b"\0" if len(payload) & 1 else b""))


def crop_wav(src: Path, dest: Path, start_s: float, end_s: float | None) -> dict:
    lay = _wav_layout(src)
    a, rate = lay["align"], lay["rate"]
    first = max(0, int(round(start_s * rate)))
    last = int(round((end_s if end_s is not None else lay["seconds"]) * rate))
    last = min(last, lay["data_size"] // a)
    if last <= first:
        raise AceStepError("crop range is empty")
    with src.open("rb") as f:
        f.seek(lay["data_offset"] + first * a)
        payload = f.read((last - first) * a)
    _write_wav(dest, lay["raw"], payload)
    return {"seconds": (last - first) / float(rate), "rate": rate, "channels": lay["channels"]}


def fit_wav(src: Path, dest: Path, seconds: float) -> dict:
    """Derived copy padded with silence / trimmed to exactly ``seconds`` (sample accurate)."""
    lay = _wav_layout(src)
    frames = int(round(seconds * lay["rate"]))
    want = frames * lay["align"]
    with src.open("rb") as f:
        f.seek(lay["data_offset"])
        payload = f.read(min(want, lay["data_size"]))
    delta = want - len(payload)
    payload += b"\0" * max(0, delta)
    _write_wav(dest, lay["raw"], payload)
    return {"frames": frames, "padded_frames": max(0, delta) // lay["align"],
            "trimmed_frames": max(0, -delta) // lay["align"]}


def analyze_wav(path: Path, remove_dc_to: Path | None = None) -> dict:
    """Peak, per-channel DC offset, NaN/Inf check for float32 / int16 WAV; optional DC-removed copy."""
    lay = _wav_layout(path)
    out: dict[str, Any] = {"seconds": round(lay["seconds"], 6), "rate": lay["rate"], "channels": lay["channels"],
                           "format": {1: "pcm", 3: "float"}.get(lay["tag"], str(lay["tag"])), "bits": lay["bits"]}
    if (lay["tag"], lay["bits"]) == (3, 32):
        code, scale = "f", 1.0
    elif (lay["tag"], lay["bits"]) == (1, 16):
        code, scale = "h", 32768.0
    else:
        out["note"] = "level analysis supports float32 and int16 WAV only"
        return out
    with path.open("rb") as f:
        f.seek(lay["data_offset"])
        samples = array.array(code)
        samples.frombytes(f.read(lay["data_size"] - lay["data_size"] % samples.itemsize))
    if sys.byteorder != "little":
        samples.byteswap()
    ch = lay["channels"]
    dc = [sum(samples[c::ch]) / max(1, len(samples) // ch) / scale for c in range(ch)]
    peak = max((abs(x) for x in samples), default=0) / scale
    bad = code == "f" and any(x != x or x in (float("inf"), float("-inf")) for x in samples[::97])
    out.update({"peak": round(peak, 6), "dc_offset": [round(x, 6) for x in dc], "non_finite": bad})
    if remove_dc_to is not None and any(abs(x) > 1e-4 for x in dc):
        if code == "f":
            for c in range(ch):
                off = dc[c]
                samples[c::ch] = array.array("f", (x - off for x in samples[c::ch]))
        else:
            for c in range(ch):
                off = dc[c] * scale
                samples[c::ch] = array.array("h", (max(-32768, min(32767, int(round(x - off))))
                                                   for x in samples[c::ch]))
        if sys.byteorder != "little":
            samples.byteswap()
        _write_wav(remove_dc_to, lay["raw"], samples.tobytes())
        out["dc_removed_copy"] = str(remove_dc_to)
    return out


# --------------------------------------------------------------------------- tasks

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def stage_reference(reference: Path, start_s: float, end_s: float | None) -> tuple[Path, dict]:
    """Crop (if asked) and copy the reference into the service's TEMP/inputs, hash-verified."""
    if not reference.is_file():
        raise AceStepError(f"reference audio not found: {reference}")
    inputs = PROCESS_TEMP / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    work = OUTPUT_DIR / "_work"
    work.mkdir(parents=True, exist_ok=True)
    if start_s or end_s is not None:
        cropped = work / f"crop-{int(time.time() * 1000)}-{reference.stem}.wav"
        info = crop_wav(reference, cropped, start_s, end_s)
        src = cropped
    else:
        info = {"seconds": _wav_layout(reference)["seconds"]}
        src = reference
    if not 10.0 <= info["seconds"] <= 120.0:
        raise AceStepError(f"reference must be 10-120 s after cropping (got {info['seconds']:.2f} s)")
    digest = _sha256(src)
    staged = inputs / f"{digest[:16]}-{src.name}"
    if staged.exists():
        if _sha256(staged) != digest:
            raise AceStepError(f"staged input {staged} differs from the source; refusing to overwrite")
    else:
        with src.open("rb") as fin, staged.open("xb") as fout:
            while chunk := fin.read(1 << 20):
                fout.write(chunk)
        if _sha256(staged) != digest:
            raise AceStepError("staged copy failed hash verification")
    info.update({"staged": str(staged), "sha256": digest})
    return staged, info


def _receipt_path(task_id: str) -> Path:
    d = OUTPUT_DIR / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{task_id}.json"


def submit_lego(staged: Path, seconds: float, track_name: str, prompt: str, bpm: float | None,
                key_scale: str | None, time_signature: str, steps: int, guidance: float, seed: int | None,
                batch_size: int, meta: dict) -> dict:
    st = ready()
    if not st.get("ready"):
        raise AceStepError(f"ACE-Step is not ready: {st}")
    if not st.get("model_ok"):
        raise AceStepError(f"service runs {st.get('model')!r}, expected {MODEL!r}")
    payload = {
        "model": MODEL, "task_type": "lego", "src_audio_path": str(staged), "track_name": track_name,
        "prompt": prompt, "lyrics": "[Instrumental]", "vocal_language": "unknown",
        "bpm": bpm, "key_scale": key_scale or "", "time_signature": time_signature,
        "audio_duration": seconds, "inference_steps": steps, "guidance_scale": guidance, "shift": 1.0,
        "repainting_start": 0.0, "repainting_end": -1.0, "batch_size": batch_size,
        "seed": seed if seed is not None else -1, "use_random_seed": seed is None, "audio_format": "wav",
        "thinking": False, "use_cot_caption": False, "use_cot_language": False,
        "sample_mode": False, "use_format": False,
    }
    if bpm is None:
        payload.pop("bpm")
    else:
        payload["bpm"] = int(round(float(bpm)))  # the service accepts integer BPM 30-300 only
        if not 30 <= payload["bpm"] <= 300:
            raise AceStepError("bpm must be between 30 and 300")
    resp = _request("/release_task", payload, timeout=60)
    task_id = (resp.get("data") or {}).get("task_id")
    if not task_id:
        raise AceStepError(f"submission returned no task id - inspect before retrying: {resp}")
    receipt = {"task_id": task_id, "request": payload, "meta": meta, "submitted_epoch": time.time(),
               "status": "submitted"}
    _receipt_path(task_id).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def poll(task_id: str, remove_dc: bool = True) -> dict:
    """One poll; downloads outputs when finished. Safe to call repeatedly."""
    rp = _receipt_path(task_id)
    receipt = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {"task_id": task_id}
    if receipt.get("status") == "succeeded" and receipt.get("outputs"):
        return receipt
    result = _request("/query_result", {"task_id_list": [task_id]})
    rows = result.get("data", [])
    if isinstance(rows, dict):
        rows = rows.get("results", rows.get("tasks", []))
    task = next((r for r in rows if r.get("task_id") == task_id), None)
    if task is None:
        raise AceStepError(f"service does not know task {task_id}: {str(result)[:300]}")
    status = task.get("status")
    if status == 0:
        receipt["status"] = "running"
        return receipt
    if status == 2:
        receipt.update(status="failed", error=task)
        rp.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return receipt
    outputs = task.get("result", [])
    if isinstance(outputs, str):
        outputs = json.loads(outputs)
    if not isinstance(outputs, list) or not outputs:
        raise AceStepError("service reported success without audio")
    name = (receipt.get("request") or {}).get("track_name", "part")
    saved = []
    for n, item in enumerate(outputs, 1):
        if item.get("status", 1) != 1:
            raise AceStepError(f"output {n} failed: {item}")
        url = item.get("file", "")
        parts = urlparse(url)
        if parts.scheme or parts.netloc or parts.path != "/v1/audio":
            raise AceStepError("unexpected non-local result URL; refusing to download")
        target = OUTPUT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}-{task_id[:8]}-{n}.wav"
        with _HTTP.open(BASE + url, timeout=180) as resp, target.open("xb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
        info = analyze_wav(target, target.with_name(target.stem + "-dc.wav") if remove_dc else None)
        use = Path(info.get("dc_removed_copy", str(target)))
        ref_seconds = ((receipt.get("meta") or {}).get("reference_info") or {}).get("seconds")
        if ref_seconds:
            aligned = use.with_name(use.stem + "-aligned.wav")
            info["aligned"] = fit_wav(use, aligned, float(ref_seconds))
            use = aligned
        saved.append({"path": str(target), "sha256": _sha256(target), "analysis": info, "use_path": str(use)})
    receipt.update(status="succeeded", outputs=saved, completed_epoch=time.time(),
                   seconds_since_submit=round(time.time() - receipt.get("submitted_epoch", time.time()), 2))
    rp.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt
