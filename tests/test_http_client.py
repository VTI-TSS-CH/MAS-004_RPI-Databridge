import unittest
from unittest.mock import patch

from mas004_rpi_databridge.http_client import HttpClient


class HttpClientTests(unittest.TestCase):
    def test_source_ip_bind_uses_httpx_string_local_address(self):
        with patch("mas004_rpi_databridge.http_client.httpx.HTTPTransport") as transport, patch(
            "mas004_rpi_databridge.http_client.httpx.Client"
        ):
            client = HttpClient(timeout_s=2.0, source_ip="10.141.94.213", verify_tls=False)

        transport.assert_called_once_with(local_address="10.141.94.213")
        client.close()


if __name__ == "__main__":
    unittest.main()
