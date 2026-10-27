"""Campus-IP deployment checks with synthetic configuration only."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.config import Config
from assistant.errors import AssistantError
from assistant.web import create_app


class DeploymentTests(unittest.TestCase):
    def test_remote_binding_requires_access_token(self):
        with self.assertRaises(AssistantError):
            Config(host="0.0.0.0").validate()
        Config(host="0.0.0.0", access_token="synthetic-access-token-long-enough").validate()

    def test_env_loads_listen_and_explicit_hosts(self):
        with patch("assistant.config.load_dotenv"), patch.dict("os.environ", {
                "ASSISTANT_HOST": "0.0.0.0", "ASSISTANT_PORT": "11209",
                "ASSISTANT_TRUSTED_HOSTS": "127.0.0.1, 192.0.2.10",
                "ASSISTANT_ACCESS_TOKEN": "synthetic-access-token-long-enough"}, clear=True):
            config = Config.from_env()
        self.assertEqual((config.host, config.port), ("0.0.0.0", 11209))
        self.assertEqual(config.trusted_hosts, ["127.0.0.1", "192.0.2.10"])

    def test_gateway_login_same_origin_and_unknown_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Config(data_dir=Path(temporary) / "data", host="0.0.0.0", port=11209,
                            trusted_hosts=["192.0.2.10"],
                            access_token="synthetic-access-token-long-enough")
            app = create_app(config)
            try:
                client = app.test_client()
                base = "http://192.0.2.10:11209"
                page = client.get("/login", base_url=base).get_data(as_text=True)
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
                self.assertEqual(client.get("/api/drafts", base_url=base).status_code, 401)
                response = client.post("/login", base_url=base,
                    data={"token": config.access_token, "csrf_token": csrf}, headers={"Origin": base})
                self.assertEqual(response.status_code, 302)
                headers = {"Origin": base, "X-CSRF-Token": csrf}
                response = client.post("/api/drafts", base_url=base, headers=headers,
                    json={"source": {"kind": "manual", "keys": []}})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(client.post("/api/drafts", base_url=base,
                    headers={**headers, "Origin": "http://other.example"}, json={}).status_code, 403)
                self.assertEqual(client.get("/login", base_url="http://other.example").status_code, 400)
            finally:
                app.extensions["mail"].close()
                app.extensions["tasks"].close()
                app.extensions["store"].close()
