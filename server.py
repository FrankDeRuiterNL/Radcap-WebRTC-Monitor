#!/usr/bin/env python3
import asyncio
import json
import os
import pathlib
import subprocess
import time
import math
from array import array
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC

Gst.init(None)

# station_idx -> set of client_ids currently monitoring
ACTIVE_LISTENERS: dict[int, set[str]] = {}
ACTIVE_LOCK = asyncio.Lock()

# ---- Tuning / stations ----
MAX_TUNERS = int(os.environ.get("RADCAP_MAX_TUNERS", "6"))
STATION_INDEXES = list(range(MAX_TUNERS))

# ---- Audio format (must match arecord output) ----
SAMPLE_RATE = int(os.environ.get("RADCAP_SR", "48000"))
CHANNELS = int(os.environ.get("RADCAP_CH", "2"))

# ---- Latency / stability target ----
# You said 250..500ms is fine; default 350ms.
TARGET_LAT_MS = int(os.environ.get("RADCAP_TARGET_LAT_MS", "350"))
QUEUE_MAX_NS = max(50, TARGET_LAT_MS) * 1_000_000  # ns

# ---- Opus / RTP ----
OPUS_BITRATE = int(os.environ.get("RADCAP_OPUS_BITRATE", "64000"))
OPUS_FRAME_MS = int(os.environ.get("RADCAP_OPUS_FRAME_MS", "20"))

BUS_POLL_MS = int(os.environ.get("RADCAP_BUS_POLL_MS", "50"))

# ---- Audio chunking + timestamping ----
BYTES_PER_SAMPLE = 2            # S16LE
FRAME_BYTES = CHANNELS * BYTES_PER_SAMPLE  # stereo = 4 bytes per frame
FRAMES_PER_CHUNK = int(SAMPLE_RATE * (OPUS_FRAME_MS / 1000.0))  # e.g. 960 @ 20ms/48k
CHUNK_BYTES = FRAMES_PER_CHUNK * FRAME_BYTES                    # e.g. 3840 bytes


def alsa_device_for_station(station_idx: int) -> str:
    return f"hw:0,0,{station_idx}"


def find_sysfs_station_path(station_idx: int) -> pathlib.Path:
    name = f"Station{station_idx:02d}"
    for p in pathlib.Path("/sys/bus/pci/devices").glob("0000:*"):
        sp = p / name
        if sp.exists():
            return sp
    raise FileNotFoundError(f"Could not find sysfs path for {name}")


def read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(errors="ignore").strip()
    except Exception:
        return ""


def write_text(path: pathlib.Path, value: str) -> None:
    with path.open("w") as f:
        f.write(value + "\n")


def station_status(station_idx: int) -> Dict:
    sp = find_sysfs_station_path(station_idx)
    return {
        "station": station_idx,
        "frequency": read_text(sp / "Frequency"),
        "rssi": read_text(sp / "RSSI"),
        "stereo": read_text(sp / "Stereo"),
    }


def station_set_frequency(station_idx: int, khz: int) -> Dict:
    sp = find_sysfs_station_path(station_idx)
    write_text(sp / "Frequency", str(khz))
    return station_status(station_idx)


def link_or_die(a: Gst.Element, b: Gst.Element, err: str):
    if not a.link(b):
        raise RuntimeError(err)


def set_if_exists(el: Gst.Element, prop: str, value):
    if el and el.find_property(prop) is not None:
        el.set_property(prop, value)
        return True
    return False


class WebRTCSender:
    """
    arecord (raw PCM S16LE) -> Python reads stdout -> appsrc (TIME + exact PTS/DURATION) ->
    queues (buffer 250..500ms) -> opusenc -> rtpopuspay -> webrtcbin
    """

    def __init__(self, station_idx: int, ws: WebSocket):
        self.station_idx = station_idx
        self.ws = ws

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.pipe: Optional[Gst.Pipeline] = None
        self.webrtc = None
        self.bus = None

        self.arecord: Optional[subprocess.Popen] = None
        self.stderr_task: Optional[asyncio.Task] = None
        self.bus_task: Optional[asyncio.Task] = None
        self.audio_task: Optional[asyncio.Task] = None

        self.appsrc: Optional[Gst.Element] = None
        self._next_pts_ns: int = 0
        self._negotiating = False

        self._last_level = None
        self._last_corr = None
        self._last_corr_ema = None
        # PCM-derived per-channel meters (robust for iOS/Safari clients)
        self._pcm_db_l = None
        self._pcm_db_r = None
        self._pcm_db_ema_l = None
        self._pcm_db_ema_r = None
        self._rtp_total_bytes = 0
        self._rtp_total_bufs = 0

    def _log(self, msg: str) -> None:
        print(f"[webrtc][st{self.station_idx:02d}] {msg}", flush=True)

    def _safe_ws_send(self, payload: dict) -> None:
        if not self.loop:
            return
        text = json.dumps(payload)

        def _send():
            asyncio.create_task(self.ws.send_text(text))

        self.loop.call_soon_threadsafe(_send)

    def _start_arecord(self) -> None:
        dev = alsa_device_for_station(self.station_idx)

        # Bigger ALSA buffers help against stutter; still reasonably low for monitoring.
        # Tune via env if needed.
        alsa_buffer_us = int(os.environ.get("RADCAP_AREC_B", str(TARGET_LAT_MS * 1000)))
        alsa_period_us = int(os.environ.get("RADCAP_AREC_F", "20000"))

        cmd = [
            "arecord",
            "-q",
            "-D", dev,
            "-r", str(SAMPLE_RATE),
            "-c", str(CHANNELS),
            "-f", "S16_LE",
            "-M",
            "-t", "raw",
            "-B", str(alsa_buffer_us),
            "-F", str(alsa_period_us),
        ]
        self.arecord = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._log(f"arecord started ({' '.join(cmd)})")

    async def _read_arecord_stderr(self):
        if not self.arecord or not self.arecord.stderr:
            return
        while True:
            line = await asyncio.to_thread(self.arecord.stderr.readline)
            if not line:
                return
            s = line.decode(errors="ignore").strip()
            if s:
                self._log(f"arecord: {s}")

    def _stop_arecord(self):
        if not self.arecord:
            return
        try:
            self._log("stopping arecord")
            self.arecord.terminate()
            try:
                self.arecord.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.arecord.kill()
        finally:
            self.arecord = None

    def _build_pipeline(self) -> Gst.Pipeline:
        pipe = Gst.Pipeline.new(f"radcap-st{self.station_idx:02d}")

        appsrc = Gst.ElementFactory.make("appsrc", "src")
        queue1 = Gst.ElementFactory.make("queue", "q1")
        audioconvert = Gst.ElementFactory.make("audioconvert", "aconv")
        level = Gst.ElementFactory.make("level", "level")
        audioresample = Gst.ElementFactory.make("audioresample", "ares")
        acaps = Gst.ElementFactory.make("capsfilter", "acaps")
        opusenc = Gst.ElementFactory.make("opusenc", "opus")
        rtpopuspay = Gst.ElementFactory.make("rtpopuspay", "pay")
        rtpcaps = Gst.ElementFactory.make("capsfilter", "rtpcaps")
        queue2 = Gst.ElementFactory.make("queue", "q2")
        webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")

        if not all([appsrc, queue1, audioconvert, level, audioresample, acaps, opusenc, rtpopuspay, rtpcaps, queue2, webrtc]):
            raise RuntimeError("Missing one or more GStreamer elements (check plugins)")

        # appsrc: hard caps + we provide timestamps based on sample count
        caps = Gst.Caps.from_string(
            f"audio/x-raw,format=S16LE,rate={SAMPLE_RATE},channels={CHANNELS},layout=interleaved"
        )
        appsrc.set_property("caps", caps)
        # Force raw caps into opusenc (avoid accidental downmix/renegotiation)
        acaps.set_property("caps", caps)
        set_if_exists(appsrc, "is-live", True)
        set_if_exists(appsrc, "format", Gst.Format.TIME)
        set_if_exists(appsrc, "do-timestamp", False)
        set_if_exists(appsrc, "block", True)

        # Stability buffering (no leaky). TARGET_LAT_MS controls max queue time.
        set_if_exists(queue1, "leaky", 0)
        set_if_exists(queue1, "max-size-time", QUEUE_MAX_NS)
        set_if_exists(queue1, "max-size-buffers", 0)
        set_if_exists(queue1, "max-size-bytes", 0)

        set_if_exists(queue2, "leaky", 0)
        set_if_exists(queue2, "max-size-time", QUEUE_MAX_NS)
        set_if_exists(queue2, "max-size-buffers", 0)
        set_if_exists(queue2, "max-size-bytes", 0)

        set_if_exists(level, "interval", 200_000_000)  # 200ms
        set_if_exists(level, "post-messages", True)

        set_if_exists(audioresample, "quality", 0)

        set_if_exists(opusenc, "bitrate", OPUS_BITRATE)
        set_if_exists(opusenc, "frame-size", OPUS_FRAME_MS)

        set_if_exists(rtpopuspay, "pt", 111)
        # Force RTP caps to advertise stereo Opus
        rtpcaps.set_property("caps", Gst.Caps.from_string(
            "application/x-rtp,media=audio,encoding-name=OPUS,payload=111,clock-rate=48000,channels=2"
        ))

        # Best-effort latency hint (property may or may not exist in your build)
        set_if_exists(webrtc, "bundle-policy", "max-bundle")
        set_if_exists(webrtc, "latency", TARGET_LAT_MS)

        for el in [appsrc, queue1, audioconvert, level, audioresample, acaps, opusenc, rtpopuspay, rtpcaps, queue2, webrtc]:
            pipe.add(el)

        link_or_die(appsrc, queue1, "link appsrc->queue1 failed")
        link_or_die(queue1, audioconvert, "link queue1->audioconvert failed")
        link_or_die(audioconvert, level, "link audioconvert->level failed")
        link_or_die(level, audioresample, "link level->audioresample failed")
        link_or_die(audioresample, acaps, "link audioresample->acaps failed")
        link_or_die(acaps, opusenc, "link acaps->opusenc failed")
        link_or_die(opusenc, rtpopuspay, "link opusenc->rtpopuspay failed")
        link_or_die(rtpopuspay, rtpcaps, "link rtpopuspay->rtpcaps failed")
        link_or_die(rtpcaps, queue2, "link rtpcaps->queue2 failed")

        # queue2.src -> webrtc request pad
        qsrc = queue2.get_static_pad("src")
        sinkpad = webrtc.get_request_pad("sink_%u")
        if not qsrc or not sinkpad:
            raise RuntimeError("could not get pads for queue2/webrtc")
        res = qsrc.link(sinkpad)
        if res != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"Failed to link queue2->webrtc: {res}")

        self.pipe = pipe
        self.webrtc = webrtc
        self.bus = self.pipe.get_bus()
        self.appsrc = appsrc
        self._next_pts_ns = 0

        self._log(f"pipeline built (TARGET_LAT_MS={TARGET_LAT_MS}ms, opus={OPUS_FRAME_MS}ms)")
        return pipe

    def _push_pcm(self, data: bytes) -> None:
        if not self.appsrc or not data:
            return

        # Align to frame boundary
        if len(data) % FRAME_BYTES != 0:
            data = data[: (len(data) // FRAME_BYTES) * FRAME_BYTES]
            if not data:
                return

        frames = len(data) // FRAME_BYTES
        duration_ns = int(frames * 1_000_000_000 / SAMPLE_RATE)

        buf = Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, data)
        buf.pts = self._next_pts_ns
        buf.dts = self._next_pts_ns
        buf.duration = duration_ns
        self._next_pts_ns += duration_ns

        try:
            self.appsrc.emit("push-buffer", buf)
        except Exception:
            pass

    async def _arecord_to_appsrc_loop(self):
        if not self.arecord or not self.arecord.stdout:
            return

        stdout = self.arecord.stdout
        pending = bytearray()

        # Read bigger blocks to reduce scheduling overhead (less stutter)
        read_bytes = CHUNK_BYTES * 8  # e.g. 160ms worth at 20ms packets

        while True:
            if self.arecord and self.arecord.poll() is not None:
                break

            chunk = await asyncio.to_thread(stdout.read, read_bytes)
            if not chunk:
                break

            pending.extend(chunk)

            while len(pending) >= CHUNK_BYTES:
                piece = bytes(pending[:CHUNK_BYTES])
                del pending[:CHUNK_BYTES]

                # Compute L/R correlation (Pearson) + PCM RMS meters (dBFS) over this chunk (stereo S16LE interleaved)
                try:
                    a = array('h')
                    a.frombytes(piece)
                    n = len(a) // 2
                    if n > 0:
                        sumL = sumR = 0.0
                        sumLL = sumRR = sumLR = 0.0
                        sumLL_pcm = sumRR_pcm = 0.0

                        # Iterate interleaved samples
                        for j in range(0, 2 * n, 2):
                            L = float(a[j])
                            R = float(a[j + 1])

                            sumL += L
                            sumR += R
                            sumLL += L * L
                            sumRR += R * R
                            sumLR += L * R
                            sumLL_pcm += L * L
                            sumRR_pcm += R * R

                        # PCM-derived RMS meters in dBFS (FS=32768 for int16)
                        rmsL = math.sqrt(sumLL_pcm / n) / 32768.0
                        rmsR = math.sqrt(sumRR_pcm / n) / 32768.0
                        eps = 1e-12
                        dbL_pcm = 20.0 * math.log10(max(rmsL, eps))
                        dbR_pcm = 20.0 * math.log10(max(rmsR, eps))
                        self._pcm_db_l = dbL_pcm
                        self._pcm_db_r = dbR_pcm
                        if self._pcm_db_ema_l is None:
                            self._pcm_db_ema_l = dbL_pcm
                            self._pcm_db_ema_r = dbR_pcm
                        else:
                            a_db = 0.12
                            self._pcm_db_ema_l = (1.0 - a_db) * self._pcm_db_ema_l + a_db * dbL_pcm
                            self._pcm_db_ema_r = (1.0 - a_db) * self._pcm_db_ema_r + a_db * dbR_pcm

                        # Correlation
                        meanL = sumL / n
                        meanR = sumR / n
                        cov = (sumLR / n) - (meanL * meanR)
                        varL = (sumLL / n) - (meanL * meanL)
                        varR = (sumRR / n) - (meanR * meanR)
                        denom = math.sqrt(max(varL, 0.0) * max(varR, 0.0))
                        if denom > 1e-9:
                            corr = max(-1.0, min(1.0, cov / denom))
                            self._last_corr = corr
                            if self._last_corr_ema is None:
                                self._last_corr_ema = corr
                            else:
                                alpha = 0.15
                                self._last_corr_ema = (1.0 - alpha) * self._last_corr_ema + alpha * corr
                except Exception:
                    pass
                self._push_pcm(piece)

        try:
            if self.appsrc:
                self.appsrc.emit("end-of-stream")
        except Exception:
            pass

    # ---- WebRTC negotiation ----
    def _on_offer_created(self, promise: Gst.Promise, element, _):
        try:
            reply = promise.get_reply()
            offer = reply.get_value("offer")
            element.emit("set-local-description", offer, Gst.Promise.new())
            sdp_text = offer.sdp.as_text()
            self._log(f"offer created (len={len(sdp_text)})")
            self._safe_ws_send({"type": "offer", "sdp": sdp_text})
        finally:
            self._negotiating = False

    def _on_ice_candidate(self, element, mlineindex, candidate):
        self._safe_ws_send({"type": "ice", "mlineindex": int(mlineindex), "candidate": str(candidate)})

    def _on_negotiation_needed(self, element):
        if self._negotiating:
            return
        self._negotiating = True
        self._log("on-negotiation-needed -> create-offer")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, self.webrtc, None)
        self.webrtc.emit("create-offer", None, promise)

    def handle_answer(self, sdp_text: str):
        res, sdpmsg = GstSdp.sdp_message_new()
        if res != GstSdp.SDPResult.OK:
            raise RuntimeError("Failed to create SDP message")
        res = GstSdp.sdp_message_parse_buffer(bytes(sdp_text, "utf-8"), sdpmsg)
        if res != GstSdp.SDPResult.OK:
            raise RuntimeError("Failed to parse SDP answer")
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())

    def add_ice(self, mlineindex: int, candidate: str):
        self.webrtc.emit("add-ice-candidate", mlineindex, candidate)

    async def _bus_poll_loop(self):
        if not self.bus:
            return
        mask = (
            Gst.MessageType.ERROR |
            Gst.MessageType.WARNING |
            Gst.MessageType.EOS |
            Gst.MessageType.ELEMENT |
            Gst.MessageType.STATE_CHANGED
        )
        last_stats = 0.0

        while True:
            msg = await asyncio.to_thread(self.bus.timed_pop_filtered, BUS_POLL_MS * Gst.MSECOND, mask)
            if msg is None:
                now = time.time()
                if now - last_stats > 0.5:
                    last_stats = now
                    self._safe_ws_send({
                        "type": "srvstats",
                        "rtp_total_bytes": self._rtp_total_bytes,
                        "rtp_total_bufs": self._rtp_total_bufs,
                        "last_level": self._last_level,
                        "corr": self._last_corr,
                        "corr_ema": self._last_corr_ema,
                        "pcm_db_l": self._pcm_db_l,
                        "pcm_db_r": self._pcm_db_r,
                        "pcm_db_ema_l": self._pcm_db_ema_l,
                        "pcm_db_ema_r": self._pcm_db_ema_r,
                    })
                continue

            t = msg.type
            if t == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                self._log(f"Gst ERROR: {err} debug={debug}")
            elif t == Gst.MessageType.WARNING:
                warn, debug = msg.parse_warning()
                self._log(f"Gst WARNING: {warn} debug={debug}")
            elif t == Gst.MessageType.EOS:
                self._log("Gst EOS")
            elif t == Gst.MessageType.ELEMENT:
                s = msg.get_structure()
                if s and s.get_name() == "level":
                    try:
                        self._last_level = {"peak": s.get_value("peak"), "rms": s.get_value("rms")}
                    except Exception:
                        pass

    async def start(self):
        self.loop = asyncio.get_running_loop()

        self._start_arecord()
        self._build_pipeline()

        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        self.webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)

        self.bus_task = asyncio.create_task(self._bus_poll_loop())
        self.stderr_task = asyncio.create_task(self._read_arecord_stderr())
        self.audio_task = asyncio.create_task(self._arecord_to_appsrc_loop())

        self.pipe.set_state(Gst.State.PLAYING)
        self._log("pipeline PLAYING")

    async def stop(self):
        try:
            for t in (self.audio_task, self.bus_task, self.stderr_task):
                if t:
                    t.cancel()
            self.audio_task = None
            self.bus_task = None
            self.stderr_task = None

            if self.pipe:
                self.pipe.set_state(Gst.State.NULL)
                self._log("pipeline NULL")
        finally:
            self.pipe = None
            self.webrtc = None
            self.bus = None
            self.appsrc = None
            self._stop_arecord()


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return HTMLResponse((pathlib.Path("static/index.html")).read_text())


@app.get("/api/stations")
def api_stations():
    return {"stations": [station_status(i) for i in STATION_INDEXES]}


@app.post("/api/stations/{station_idx}/frequency")
async def api_set_freq(station_idx: int, payload: Dict):
    if station_idx not in STATION_INDEXES:
        return {"error": "station out of range"}
    try:
        khz = int(payload.get("khz", 0))
    except Exception:
        return {"error": "invalid khz"}
    if khz < 87000 or khz > 108000:
        return {"error": "khz out of FM band (87000..108000)"}
    try:
        return station_set_frequency(station_idx, khz)
    except Exception as e:
        return {"error": f"failed to set frequency: {e}"}


@app.websocket("/ws/status")
async def ws_status(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            client_id = ws.query_params.get("client_id", "")
            stations = [station_status(i) for i in STATION_INDEXES]
            async with ACTIVE_LOCK:
                for st in stations:
                    sidx = st.get("station")
                    listeners = ACTIVE_LISTENERS.get(sidx, set())
                    st["listeners"] = len(listeners)
                    st["busy"] = (len(listeners) > 0) and (client_id not in listeners)
            data = {"ts": time.time(), "stations": stations}
            await ws.send_text(json.dumps(data))
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return


@app.websocket("/ws/webrtc/{station_idx}")
async def ws_webrtc(ws: WebSocket, station_idx: int):
    if station_idx not in STATION_INDEXES:
        await ws.close()
        return

    await ws.accept()
    client_id = ws.query_params.get("client_id", "")
    async with ACTIVE_LOCK:
        s = ACTIVE_LISTENERS.setdefault(station_idx, set())
        if client_id:
            s.add(client_id)
    sender = WebRTCSender(station_idx, ws)

    try:
        await sender.start()
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            if data.get("type") == "answer":
                sender.handle_answer(data["sdp"])
            elif data.get("type") == "ice":
                sender.add_ice(int(data["mlineindex"]), data["candidate"])
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[webrtc][st{station_idx:02d}] WS handler error: {e}", flush=True)
    finally:
        async with ACTIVE_LOCK:
            s = ACTIVE_LISTENERS.get(station_idx)
            if s and client_id in s:
                s.remove(client_id)
                if not s:
                    ACTIVE_LISTENERS.pop(station_idx, None)
        await sender.stop()
        try:
            await ws.close()
        except Exception:
            pass
