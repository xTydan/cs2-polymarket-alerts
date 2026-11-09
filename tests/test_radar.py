import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
import requests

from radar import Radar, Database, DEFAULT, EventMarket, ApiClient, classify, is_cs2

COND = "0x" + "a" * 64


class FakeAPI:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.messages = []
        self.fail_telegram = False

    def get(self, base, path, params=None):
        if path == "/sports":
            return [{"sport": "cs2", "tags": "123"}]
        if path == "/events/keyset":
            if params.get("after_cursor"):
                return {"events": [], "next_cursor": None}
            return {"events": [{
                "id": "99", "title": "Counter-Strike: Team A vs Team B (BO3) - CCT Europe",
                "slug": "fake-test-match", "volume": 4000,
                "markets": [{"conditionId": COND, "question": "Team A vs Team B", "volume": 4000,
                             "outcomes": '["Team A", "Team B"]', "closed": False}]
            }], "next_cursor": None}
        if path == "/v2/trades":
            return {"data": list(reversed(self.rows)), "pagination": {"next_cursor": None}}
        if path == "/v2/user-volume":
            return {"data": {"trade_count": 2, "volume_usdc": 1222}}
        raise Exception(path)

    def post_telegram(self, token, chat_id, message):
        if self.fail_telegram:
            raise ConnectionError("Telegram temporarily unavailable")
        self.messages.append(message)


def trade(wallet: str, usd: float, timestamp: int, txn: str):
    price = 0.5
    return {"condition_id": COND, "proxy_wallet": wallet, "side": "BUY", "price": price,
            "size": usd / price, "timestamp": timestamp, "transaction_hash": txn,
            "token_id": "111", "outcome": "Team B"}


class SecurityTests(unittest.TestCase):
    def test_telegram_token_is_not_exposed_on_network_error(self):
        client = ApiClient()
        token = "123456789:SENSITIVE-TOKEN"
        client.session = Mock()
        client.session.post.side_effect = requests.ConnectionError(
            "HTTPSConnectionPool(host='api.telegram.org'): URL /bot" + token + "/sendMessage")
        with self.assertRaises(RuntimeError) as error:
            client.post_telegram(token, "12", "test")
        self.assertNotIn(token, str(error.exception))


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / "db.sqlite"))
        self.addCleanup(self.db.conn.close)
        self.cfg = DEFAULT.copy()
        self.cfg["wallet_history_check"] = False
        # These alert tests must not depend on 21:00 local-time daily report.
        self.cfg["daily_summary_enabled"] = False
        self.cfg["multi_wallet_cluster_usd"] = 900
        # Testy natychmiastowych alertów: w produkcji progi pozostają bez zmian.
        # W teście ustawiamy kwoty tak, aby odróżnić PILNE od zbiorczych.
        self.cfg["priority_threshold_multiplier"] = 1.0
        self.cfg["priority_min_usd"] = 1000
        self.cfg["priority_cluster_usd"] = 1200
        self.cfg["wallet_cluster_usd"] = 1000
        self.api = FakeAPI()
        self.bot = Radar(self.api, self.db, self.cfg, "TOKEN", "123", False)
        self.bot.csv_path = Path(self.temp.name) / "alerty.csv"
        self.bot.cache_path = Path(self.temp.name) / "market_cache.json"
        self.bot.last_discovery = 0
        self.bot.markets = {
            COND: EventMarket(COND, "99", "Counter-Strike: Team A vs Team B (BO3) - CCT Europe",
                              "Team A vs Team B", "fake-test-match", 4000, 1500, "lower", ["Team A", "Team B"])
        }
        self.bot.event_ids = ["99"]

    def test_discovery_does_not_include_other_esports_or_major(self):
        self.bot.discover()
        self.assertEqual(self.bot.event_ids, ["99"])
        self.assertEqual(self.bot.markets[COND].tier, "lower")
        self.assertTrue(is_cs2("Counter-Strike"))
        self.assertEqual(classify("IEM Cologne - BO3"), "major")

    def test_discovery_keyset_paginates_beyond_2100_without_offset(self):
        # Weryfikacja naprawy błędu 422 przy offset=2100 z oryginalnej wersji.
        class ManyPages(FakeAPI):
            def __init__(self):
                super().__init__()
                self.requests = []

            def get(self, base, path, params=None):
                if path == "/events/keyset":
                    self.requests.append(params.copy())
                    assert "offset" not in params
                    ix = int(params.get("after_cursor", "0"))
                    if ix >= 22:
                        return {"events": [], "next_cursor": None}
                    return {"events": [{"id": str(ix * 100 + k), "title": "Other sports event",
                                        "slug": "other-sports-event", "markets": []}
                                       for k in range(100)], "next_cursor": str(ix + 1)}
                return super().get(base, path, params)

        self.bot.client = ManyPages()
        self.bot.discover()
        self.assertEqual(len(self.bot.client.requests), 23)
        self.assertEqual(self.bot.client.requests[21]["after_cursor"], "21")
        self.assertTrue(all("offset" not in req for req in self.bot.client.requests))

    def test_cs2_slug_identifies_match_without_game_in_title(self):
        class SlugAPI(FakeAPI):
            def get(self, base, path, params=None):
                if path == "/events/keyset":
                    return {"events": [{
                        "id": "99", "title": "Team A vs Team B", "slug": "cs2-team-a-vs-team-b",
                        "volume": 1000,
                        "markets": [{"conditionId": COND, "question": "Team A vs Team B",
                                     "volume": 1000, "outcomes": '["Team A", "Team B"]',
                                     "closed": False}]
                    }], "next_cursor": None}
                return super().get(base, path, params)

        self.bot.client = SlugAPI()
        self.bot.discover()
        self.assertEqual(len(self.bot.markets), 1)
        self.assertIn(COND, self.bot.markets)

    def test_discovery_cache_saves_and_restores(self):
        self.bot.discover()
        self.assertTrue(self.bot.cache_path.exists())
        restored = Radar(self.api, self.db, self.cfg, "TOKEN", "123", False)
        restored.cache_path = self.bot.cache_path
        restored.load_market_cache()
        self.assertEqual(restored.event_ids, ["99"])
        self.assertEqual(len(restored.markets), 1)
        self.assertGreater(restored.last_discovery, 0)

    def test_delayed_scan_catches_prior_trades(self):
        now = int(time.time())
        self.db.set_meta('last_successful_poll', str(now - 1200))
        self.api.rows = [trade("0x" + "1" * 40, 1500, now - 1000, "0xlate")]
        self.bot.started = now
        self.bot.poll()
        self.assertEqual(len(self.api.messages), 1)

    def test_discovery_repeated_cursor_stops(self):
        class BadCursor(FakeAPI):
            def get(self, base, path, params=None):
                if path == "/events/keyset":
                    return {"events": [{"id": "123", "title": "Other",
                                        "slug": "other", "markets": []}],
                            "next_cursor": "same"}
                return super().get(base, path, params)

        self.bot.client = BadCursor()
        self.bot.discover()
        self.assertEqual(self.bot.event_ids, [])

    def test_detects_large_purchase_and_avoids_duplicates(self):
        now = int(time.time())
        row = trade("0x" + "1" * 40, 1500, now, "0xabc")
        self.assertTrue(self.bot.process(row, now-30))
        self.bot.flush_outbox()
        self.assertEqual(len(self.api.messages), 1)
        self.assertIn("$1,500.00", self.api.messages[0])
        self.assertFalse(self.bot.process(row, now-30))
        self.bot.flush_outbox()
        self.assertEqual(len(self.api.messages), 1)

    def test_multiwallet_cluster(self):
        now = int(time.time())
        for i in range(3):
            self.bot.process(trade("0x" + str(i+1)*40, 500, now+i, f"0x{i}"), now-30)
        self.bot.flush_outbox()
        self.assertEqual(len(self.api.messages), 1)
        self.assertIn("KILKA PORTFELI", self.api.messages[0])
        self.assertIn("$1,500.00", self.api.messages[0])

    def test_wallet_cluster(self):
        now = int(time.time())
        wallet = "0x" + "1" * 40
        self.bot.process(trade(wallet, 600, now, "0x1"), now-30)
        self.bot.process(trade(wallet, 600, now + 1, "0x2"), now-30)
        self.bot.flush_outbox()
        self.assertEqual(len(self.api.messages), 1)
        self.assertIn("PORTFEL ZBUDOWAŁ DUŻY WOLUMEN ZAKUPÓW", self.api.messages[0])

    def test_outbox_retries_failed_message(self):
        now = int(time.time())
        self.bot.process(trade("0x" + "1" * 40, 1500, now, "0xfailed"), now-30)
        self.api.fail_telegram = True
        self.bot.flush_outbox()
        self.assertEqual(len(self.api.messages), 0)
        self.assertEqual(len(self.db.pending_alerts()), 1)
        self.api.fail_telegram = False
        self.bot.flush_outbox()
        self.assertEqual(len(self.api.messages), 1)
        self.assertEqual(len(self.db.pending_alerts()), 0)

    def test_poll_uses_public_v2_api_and_deduplicates(self):
        now = int(time.time())
        self.api.rows = [trade("0x" + "1" * 40, 1500, now, "0xone")]
        self.bot.poll()
        self.bot.poll()
        self.assertEqual(len(self.api.messages), 1)


if __name__ == "__main__":
    unittest.main()
