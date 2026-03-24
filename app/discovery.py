from __future__ import annotations

import ipaddress
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, Iterator, List, Optional, Sequence

from app.models import DlnaDevice, DlnaService

SSDP_ADDRESS = ("239.255.255.250", 1900)
SEARCH_TARGETS = [
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:service:AVTransport:1",
    "urn:schemas-upnp-org:service:RenderingControl:1",
    "upnp:rootdevice",
    "ssdp:all",
]
BROAD_SEARCH_TARGETS = {"ssdp:all", "upnp:rootdevice"}
SSDP_TEMPLATE = "\r\n".join(
    [
        "M-SEARCH * HTTP/1.1",
        "HOST: 239.255.255.250:1900",
        'MAN: "ssdp:discover"',
        "MX: 2",
        "ST: {st}",
        "",
        "",
    ]
)
RECV_TIMEOUT = 0.15
MIN_DESCRIPTION_FETCH_TIMEOUT = 0.2
MAX_DESCRIPTION_FETCH_TIMEOUT = 2.0
MIN_DESCRIPTION_FETCH_RESERVE = 0.75
MAX_DESCRIPTION_FETCH_RESERVE = 1.5


def parse_ssdp_response(data: bytes) -> Dict[str, str]:
    text = data.decode("utf-8", errors="ignore")
    headers: Dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def _find_text(element: Optional[ET.Element], path: str) -> str:
    if element is None:
        return ""
    return element.findtext(path, default="").strip()


def _base_url_from_location(location: str, url_base: str) -> str:
    if url_base:
        return url_base.strip()
    parsed = urllib.parse.urlparse(location)
    return f"{parsed.scheme}://{parsed.netloc}"


def _iter_device_nodes(device: Optional[ET.Element]) -> Iterator[ET.Element]:
    if device is None:
        return
    yield device
    for child in device.findall("{*}deviceList/{*}device"):
        yield from _iter_device_nodes(child)


def _extract_services(device: Optional[ET.Element], base_url: str) -> tuple[Optional[DlnaService], Optional[DlnaService]]:
    av_transport = None
    rendering_control = None
    if device is None:
        return av_transport, rendering_control

    for service in device.findall("{*}serviceList/{*}service"):
        service_type = _find_text(service, "{*}serviceType")
        control_url = urllib.parse.urljoin(base_url, _find_text(service, "{*}controlURL"))
        event_sub_url = urllib.parse.urljoin(base_url, _find_text(service, "{*}eventSubURL"))
        scpd_url = urllib.parse.urljoin(base_url, _find_text(service, "{*}SCPDURL"))
        info = DlnaService(
            service_type=service_type,
            control_url=control_url,
            event_sub_url=event_sub_url,
            scpd_url=scpd_url,
        )
        if "AVTransport" in service_type and av_transport is None:
            av_transport = info
        if "RenderingControl" in service_type and rendering_control is None:
            rendering_control = info
    return av_transport, rendering_control


def _device_hint(device: Optional[ET.Element], headers: Dict[str, str], usn: str) -> str:
    parts = [
        _find_text(device, "{*}deviceType"),
        _find_text(device, "{*}friendlyName"),
        _find_text(device, "{*}manufacturer"),
        _find_text(device, "{*}modelName"),
        headers.get("st", ""),
        headers.get("server", ""),
        usn,
    ]
    return " ".join(part for part in parts if part).lower()


def _select_best_device(
    root_device: Optional[ET.Element],
    base_url: str,
    headers: Dict[str, str],
    usn: str,
) -> tuple[Optional[ET.Element], Optional[DlnaService], Optional[DlnaService]]:
    best_device = root_device
    best_av_transport, best_rendering_control = _extract_services(root_device, base_url)
    best_score = -1

    for candidate in _iter_device_nodes(root_device):
        av_transport, rendering_control = _extract_services(candidate, base_url)
        hint = _device_hint(candidate, headers, usn)
        score = 0
        if "mediarenderer" in hint:
            score += 4
        if av_transport is not None:
            score += 4
        if rendering_control is not None:
            score += 2
        if _find_text(candidate, "{*}friendlyName"):
            score += 1
        if score > best_score:
            best_score = score
            best_device = candidate
            best_av_transport = av_transport
            best_rendering_control = rendering_control

    return best_device, best_av_transport, best_rendering_control


def parse_device_description_xml(
    location: str,
    usn: str,
    headers: Dict[str, str],
    xml_text: str,
) -> DlnaDevice:
    root = ET.fromstring(xml_text)
    root_device = root.find(".//{*}device")
    base_url = _base_url_from_location(location, root.findtext(".//{*}URLBase", default=""))
    device, av_transport, rendering_control = _select_best_device(root_device, base_url, headers, usn)

    return DlnaDevice(
        location=location,
        usn=usn,
        friendly_name=_find_text(device, "{*}friendlyName") or headers.get("server", "Unknown device"),
        manufacturer=_find_text(device, "{*}manufacturer"),
        model_name=_find_text(device, "{*}modelName"),
        device_type=_find_text(device, "{*}deviceType"),
        base_url=base_url,
        av_transport=av_transport,
        rendering_control=rendering_control,
        raw_headers=headers,
    )


def _is_probable_renderer(device: DlnaDevice, headers: Dict[str, str], usn: str) -> bool:
    discovery_hint = " ".join(
        part
        for part in [
            device.device_type,
            headers.get("st", ""),
            headers.get("server", ""),
            usn,
            device.friendly_name,
            device.manufacturer,
            device.model_name,
        ]
        if part
    ).lower()
    return device.supports_media_cast or device.rendering_control is not None or "mediarenderer" in discovery_hint


def _discovery_hint_from_headers(headers: Dict[str, str], usn: str) -> str:
    return " ".join(
        part for part in [headers.get("st", ""), headers.get("server", ""), usn] if part
    ).lower()


def _discovery_candidate_priority(headers: Dict[str, str], usn: str) -> int:
    hint = _discovery_hint_from_headers(headers, usn)
    score = 0
    if "mediarenderer" in hint:
        score += 12
    if "avtransport" in hint:
        score += 8
    if "renderingcontrol" in hint:
        score += 6
    if "dlna" in hint or "dmr" in hint:
        score += 2
    return score


def _split_search_targets(search_targets: Sequence[str]) -> tuple[list[str], list[str]]:
    targeted = [target for target in search_targets if target not in BROAD_SEARCH_TARGETS]
    if not targeted:
        return list(search_targets), []
    fallback = [target for target in search_targets if target in BROAD_SEARCH_TARGETS]
    return targeted, fallback


def _description_fetch_reserve(timeout: float) -> float:
    return min(MAX_DESCRIPTION_FETCH_RESERVE, max(MIN_DESCRIPTION_FETCH_RESERVE, timeout * 0.25))


def _description_fetch_timeout(deadline: float, pending_count: int) -> float:
    remaining = max(deadline - time.monotonic(), 0.0)
    if remaining <= 0:
        return 0.0
    if pending_count <= 1:
        return min(MAX_DESCRIPTION_FETCH_TIMEOUT, remaining)
    if remaining <= MIN_DESCRIPTION_FETCH_TIMEOUT:
        return remaining
    return min(MAX_DESCRIPTION_FETCH_TIMEOUT, max(MIN_DESCRIPTION_FETCH_TIMEOUT, remaining / pending_count))


def _sorted_discovery_results(
    results: Dict[str, tuple[Dict[str, str], str]]
) -> list[tuple[str, tuple[Dict[str, str], str]]]:
    items = list(results.items())
    items.sort(key=lambda item: (-_discovery_candidate_priority(item[1][0], item[1][1]), item[0]))
    return items


def fetch_device_description(location: str, usn: str, headers: Dict[str, str], timeout: float = 3.0) -> Optional[DlnaDevice]:
    request = urllib.request.Request(location, headers={"User-Agent": "ScreenCasting/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            xml_text = response.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None

    try:
        device = parse_device_description_xml(location, usn, headers, xml_text)
    except ET.ParseError:
        return None

    if not _is_probable_renderer(device, headers, usn):
        return None
    return device


def _is_usable_ipv4(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local:
        return False
    return True


def iter_local_ipv4_addresses() -> List[str]:
    addresses: List[str] = []

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            preferred = probe.getsockname()[0]
            if _is_usable_ipv4(preferred):
                addresses.append(preferred)
    except OSError:
        pass

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
    except socket.gaierror:
        infos = []

    for info in infos:
        ip = info[4][0]
        if _is_usable_ipv4(ip) and ip not in addresses:
            addresses.append(ip)
    return addresses


def _create_search_socket(local_ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((local_ip, 0))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
    sock.settimeout(RECV_TIMEOUT)
    return sock


def discover_devices(timeout: float = 5.0, search_targets: Sequence[str] = SEARCH_TARGETS) -> List[DlnaDevice]:
    timeout = max(float(timeout), 0.5)
    deadline = time.monotonic() + timeout
    results: Dict[str, tuple[Dict[str, str], str]] = {}
    local_ips = iter_local_ipv4_addresses()
    sockets: List[socket.socket] = []

    try:
        for local_ip in local_ips:
            try:
                sockets.append(_create_search_socket(local_ip))
            except OSError:
                continue

        if not sockets:
            fallback = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            fallback.settimeout(RECV_TIMEOUT)
            sockets.append(fallback)

        targeted_targets, fallback_targets = _split_search_targets(search_targets)
        targeted_payloads = [SSDP_TEMPLATE.format(st=target).encode("utf-8") for target in targeted_targets]
        fallback_payloads = [SSDP_TEMPLATE.format(st=target).encode("utf-8") for target in fallback_targets]
        rounds = max(2, min(4, int(timeout) + 1))
        round_interval = max(0.8, timeout / max(rounds, 1))
        fetch_reserve = _description_fetch_reserve(timeout)
        next_send_at = time.monotonic()
        sent_rounds = 0

        while time.monotonic() < deadline:
            now = time.monotonic()
            if results and deadline - now <= fetch_reserve:
                break
            if sent_rounds < rounds and now >= next_send_at:
                payloads = targeted_payloads
                if not results and fallback_payloads and sent_rounds >= rounds - 1:
                    payloads = targeted_payloads + fallback_payloads
                for sock in sockets:
                    for payload in payloads:
                        try:
                            sock.sendto(payload, SSDP_ADDRESS)
                        except OSError:
                            continue
                sent_rounds += 1
                next_send_at = now + round_interval

            for sock in sockets:
                try:
                    data, _ = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    continue
                headers = parse_ssdp_response(data)
                location = headers.get("location")
                usn = headers.get("usn", "")
                if not location:
                    continue
                existing = results.get(location)
                if existing is not None:
                    current_score = _discovery_candidate_priority(headers, usn)
                    existing_score = _discovery_candidate_priority(existing[0], existing[1])
                    if current_score <= existing_score:
                        continue
                results[location] = (headers, usn)
    finally:
        for sock in sockets:
            sock.close()

    devices: List[DlnaDevice] = []
    candidates = _sorted_discovery_results(results)
    for index, (location, (headers, usn)) in enumerate(candidates):
        fetch_timeout = _description_fetch_timeout(deadline, len(candidates) - index)
        if fetch_timeout <= 0:
            break
        device = fetch_device_description(location, usn, headers, timeout=fetch_timeout)
        if device is not None:
            devices.append(device)
    devices.sort(key=lambda item: item.display_name.lower())
    return devices
