"""Group list from the admin app, with fallbacks. Run: python -m unittest discover tests"""

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from harvestradar.groups import ids_by_name, resolve
from harvestradar.leads_sync import to_row

URL = "https://x/functions/v1/ingest-chat-requests"
G1 = {"id": "11111111-1111-4111-8111-111111111111", "name": "Atl活动"}


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def server(groups):
    def handle(request: httpx.Request):
        if request.headers.get("x-ingest-token") != "tok":
            return httpx.Response(401, json={"error": "bad token"})
        return httpx.Response(200, json={"groups": groups})
    return handle


def down(request):
    return httpx.Response(503, json={"error": "down"})


class GroupsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def test_admin_list_wins_and_is_saved(self):
        groups, source = resolve(self.out, ["本地群"], URL, "tok", client(server([G1])))
        self.assertEqual((groups, source), ([G1], "后台群名单"))
        self.assertEqual(json.loads((self.out / "groups.json").read_text()), [G1])

    def test_admin_down_uses_last_saved_copy(self):
        resolve(self.out, [], URL, "tok", client(server([G1])))
        groups, source = resolve(self.out, ["本地群"], URL, "tok", client(down))
        self.assertEqual(groups, [G1])
        self.assertIn("上次存下的", source)

    def test_never_reached_admin_uses_collector_toml(self):
        groups, source = resolve(self.out, ["本地群"], URL, "tok", client(down))
        self.assertEqual((groups, source), ([{"id": None, "name": "本地群"}], "collector.toml"))

    def test_no_settings_uses_collector_toml(self):
        self.assertEqual(resolve(self.out, ["本地群"], "", "")[1], "collector.toml")

    def test_wrong_token_is_not_saved_as_an_empty_list(self):
        resolve(self.out, ["本地群"], URL, "nope", client(server([G1])))
        self.assertFalse((self.out / "groups.json").exists())

    def test_all_paused_means_nothing_to_grab(self):
        groups, source = resolve(self.out, ["本地群"], URL, "tok", client(server([])))
        self.assertEqual((groups, source), ([], "后台群名单"))

    def test_requests_are_tagged_with_the_group_id(self):
        resolve(self.out, [], URL, "tok", client(server([G1])))
        ids = ids_by_name(self.out)
        lead = {"group": "Atl活动", "sender": "Alex", "quote": "求 beta"}
        self.assertEqual(to_row(lead, ids)["group_id"], G1["id"])
        self.assertIsNone(to_row({**lead, "group": "别的群"}, ids)["group_id"])


if __name__ == "__main__":
    unittest.main()
