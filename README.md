# Radcap WebRTC Monitor

A simple webinterface hosted on linux using FastAPI/WebRTC to monitor the output of a Sonifex PC-FM6-32 FM capture card and tune the stations it receives.
Also provides meters in dBFS scale and RSSI (receiving strenght) meter aswell as a correlation meter.

## Description

I've only tested this locally in a Ubuntu Server VM with PCIe passtrough to the VM.
The code is fully written by ChatGPT, he takes all the blame if it sucks.
Works fine on Google Chrome desktop, has some tweaks for iOS but no fully fixed.

## Getting Started

### Dependencies

```
sudo apt-get update
sudo apt-get install -y \
  build-essential pkg-config ca-certificates curl wget git \
  python3 python3-venv python3-pip python3-gi python3-gi-cairo \
  alsa-utils libasound2 libasound2-dev \
  gstreamer1.0-tools gstreamer1.0-alsa \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  gir1.2-gst-plugins-base-1.0 \
  gir1.2-gst-plugins-bad-1.0 \
  gir1.2-gst-webrtc-1.0 \
  libopus0 libopus-dev
```

### Executing Monitor

```
RADCAP_TARGET_LAT_MS=350 sudo python3 -m uvicorn server:app --host 0.0.0.0 --port 8080
```

### Nginx Proxy Manager - Advanced Settings
Needed when behind reverse proxy.

```
# WebSockets (/ws/status + /ws/webrtc/..)
location /ws/ {
  proxy_pass http://192.168.1.172:8080;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_read_timeout 3600s;
  proxy_send_timeout 3600s;
  proxy_buffering off;
}

# (Optioneel) algemene timeouts voor HTTP requests
proxy_read_timeout 120s;
proxy_send_timeout 120s;
```

### Interface Example

![WebRTC Monitor](https://raw.githubusercontent.com/FrankDeRuiterNL/Radcap-WebRTC-Monitor/refs/heads/main/readme/radcap_webrtc_monitor.png)

## Links

Sonifex PC-FM6-32 Hardware: [Manufacturer Website](https://www.sonifex.co.uk/radiocards/pc-fm6-32.shtml)

Sonifex PC-FM6-32 Driver: [Manufacturer Website](https://www.sonifex.co.uk/technical/software/index.shtml#radiocapturecards)
