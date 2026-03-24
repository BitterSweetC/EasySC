from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(slots=True)
class DlnaService:
    service_type: str
    control_url: str
    event_sub_url: str = ""
    scpd_url: str = ""


@dataclass(slots=True)
class DlnaDevice:
    location: str
    usn: str
    friendly_name: str
    manufacturer: str = ""
    model_name: str = ""
    device_type: str = ""
    base_url: str = ""
    av_transport: Optional[DlnaService] = None
    rendering_control: Optional[DlnaService] = None
    raw_headers: Dict[str, str] = field(default_factory=dict)

    @property
    def supports_media_cast(self) -> bool:
        return self.av_transport is not None

    @property
    def display_name(self) -> str:
        parts = [self.friendly_name.strip() or "Unknown device"]
        detail = " ".join(part for part in [self.manufacturer.strip(), self.model_name.strip()] if part)
        if detail:
            parts.append(f"({detail})")
        return " ".join(parts)
