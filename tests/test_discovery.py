import unittest

from app.discovery import parse_device_description_xml, parse_ssdp_response


SAMPLE_RESPONSE = b"""HTTP/1.1 200 OK\r
CACHE-CONTROL: max-age=1800\r
DATE: Sat, 21 Mar 2026 10:00:00 GMT\r
EXT:\r
LOCATION: http://192.168.1.8:8895/description.xml\r
SERVER: TestTV/1.0 UPnP/1.0 DLNADOC/1.50\r
ST: urn:schemas-upnp-org:device:MediaRenderer:1\r
USN: uuid:test-tv::urn:schemas-upnp-org:device:MediaRenderer:1\r
\r
"""

SAMPLE_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <URLBase>http://192.168.1.8:8895/</URLBase>
  <device>
    <friendlyName>Living Room TV</friendlyName>
    <manufacturer>OpenAI</manufacturer>
    <modelName>Demo TV</modelName>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <controlURL>/MediaRenderer/AVTransport/Control</controlURL>
        <eventSubURL>/MediaRenderer/AVTransport/Event</eventSubURL>
        <SCPDURL>/AVTransport.xml</SCPDURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <controlURL>/MediaRenderer/RenderingControl/Control</controlURL>
        <eventSubURL>/MediaRenderer/RenderingControl/Event</eventSubURL>
        <SCPDURL>/RenderingControl.xml</SCPDURL>
      </service>
    </serviceList>
  </device>
</root>
"""

EMBEDDED_RENDERER_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <URLBase>http://192.168.1.20:8895/</URLBase>
  <device>
    <friendlyName>Smart Box</friendlyName>
    <manufacturer>OpenAI</manufacturer>
    <modelName>Root Device</modelName>
    <deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>
    <deviceList>
      <device>
        <friendlyName>Tmall Magic Box</friendlyName>
        <manufacturer>Tmall</manufacturer>
        <modelName>M17</modelName>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
            <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
            <controlURL>/dmr/avtransport</controlURL>
            <eventSubURL>/dmr/avtransport/event</eventSubURL>
            <SCPDURL>/dmr/avtransport.xml</SCPDURL>
          </service>
        </serviceList>
      </device>
    </deviceList>
  </device>
</root>
"""


class DiscoveryTests(unittest.TestCase):
    def test_parse_ssdp_response(self) -> None:
        headers = parse_ssdp_response(SAMPLE_RESPONSE)
        self.assertEqual(headers["location"], "http://192.168.1.8:8895/description.xml")
        self.assertIn("mediarenderer", headers["st"].lower())

    def test_parse_device_description_xml(self) -> None:
        headers = parse_ssdp_response(SAMPLE_RESPONSE)
        device = parse_device_description_xml(
            location=headers["location"],
            usn=headers["usn"],
            headers=headers,
            xml_text=SAMPLE_XML,
        )
        self.assertEqual(device.display_name, "Living Room TV (OpenAI Demo TV)")
        self.assertIsNotNone(device.av_transport)
        self.assertEqual(
            device.av_transport.control_url,  # type: ignore[union-attr]
            "http://192.168.1.8:8895/MediaRenderer/AVTransport/Control",
        )

    def test_parse_device_description_xml_prefers_embedded_renderer(self) -> None:
        headers = parse_ssdp_response(SAMPLE_RESPONSE)
        device = parse_device_description_xml(
            location="http://192.168.1.20:8895/description.xml",
            usn=headers["usn"],
            headers=headers,
            xml_text=EMBEDDED_RENDERER_XML,
        )
        self.assertEqual(device.friendly_name, "Tmall Magic Box")
        self.assertEqual(device.manufacturer, "Tmall")
        self.assertIsNotNone(device.av_transport)
        self.assertEqual(
            device.av_transport.control_url,  # type: ignore[union-attr]
            "http://192.168.1.20:8895/dmr/avtransport",
        )


if __name__ == "__main__":
    unittest.main()
