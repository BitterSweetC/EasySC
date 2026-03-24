from __future__ import annotations

import html
import urllib.request
from typing import Iterable, Tuple

from app.media_source import display_name_from_source, guess_content_type_from_source
from app.models import DlnaDevice, DlnaService


def guess_content_type(file_path: str) -> str:
    return guess_content_type_from_source(file_path)


def build_didl_metadata(file_path: str, media_url: str) -> str:
    title = html.escape(display_name_from_source(file_path))
    protocol_info = f"http-get:*:{guess_content_type(file_path)}:*"
    resource = html.escape(media_url)
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{title}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="{protocol_info}">{resource}</res>'
        "</item>"
        "</DIDL-Lite>"
    )


def build_action_envelope(service_type: str, action: str, fields: Iterable[Tuple[str, str]]) -> bytes:
    body = "".join(f"<{name}>{html.escape(value)}</{name}>" for name, value in fields)
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{html.escape(service_type)}">{body}</u:{action}>'
        "</s:Body>"
        "</s:Envelope>"
    )
    return envelope.encode("utf-8")


class DlnaController:
    def __init__(self, device: DlnaDevice):
        self.device = device

    def _post_action(self, service: DlnaService, action: str, fields: Iterable[Tuple[str, str]]) -> bytes:
        request = urllib.request.Request(
            service.control_url,
            data=build_action_envelope(service.service_type, action, fields),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{service.service_type}#{action}"',
                "User-Agent": "ScreenCasting/0.2",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read()

    def set_media(self, media_url: str, file_path: str) -> bytes:
        if self.device.av_transport is None:
            raise ValueError("目标设备不支持 AVTransport")
        metadata = build_didl_metadata(file_path, media_url)
        return self._post_action(
            self.device.av_transport,
            "SetAVTransportURI",
            [
                ("InstanceID", "0"),
                ("CurrentURI", media_url),
                ("CurrentURIMetaData", metadata),
            ],
        )

    def play(self, speed: str = "1") -> bytes:
        if self.device.av_transport is None:
            raise ValueError("目标设备不支持 AVTransport")
        return self._post_action(
            self.device.av_transport,
            "Play",
            [
                ("InstanceID", "0"),
                ("Speed", speed),
            ],
        )

    def stop(self) -> bytes:
        if self.device.av_transport is None:
            raise ValueError("目标设备不支持 AVTransport")
        return self._post_action(self.device.av_transport, "Stop", [("InstanceID", "0")])

    def set_volume(self, volume: int) -> bytes:
        if self.device.rendering_control is None:
            raise ValueError("目标设备不支持音量控制")
        clamped = max(0, min(100, volume))
        return self._post_action(
            self.device.rendering_control,
            "SetVolume",
            [
                ("InstanceID", "0"),
                ("Channel", "Master"),
                ("DesiredVolume", str(clamped)),
            ],
        )
