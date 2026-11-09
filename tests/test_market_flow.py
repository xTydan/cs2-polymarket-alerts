import tempfile
import time
import unittest
from pathlib import Path

from market_flow import assess_market_flow
from radar import Database, DEFAULT, EventMarket, Radar

COND1 = "0x" + "a" * 64
COND2 = "0x" + "b" * 64


def flow_rows(now, side_a=300, side_b=2700, wallets=3):
    result = []
    for ix in range(wallets):
        result.append((now - 60 + ix, "Team B", f"wallet-{ix}", side_b / wallets, 0.5))
    result.append((now - 30, "Team A", "other", side_a, 0.5))
    return result


def tx(cond, wallet, amount, timestamp, trade_id, outcome="Team B"):
    return {
        "id": trade_id, "timestamp": timestamp, "condition_id": cond,
        "token_id": "token", "wallet": wallet, "outcome": outcome,
        "trader_name": "", "price": 0.5, "usd": amount,
    }


class MarketFlowMathTests(unittest.TestCase):
    def test_strong_skew_with_multiple_wallets(self):
        now = 1800000000
        flows = assess_market_flow(flow_rows(now), ["Team A", "Team B"], now)
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["outcome"], "Team B")
        self.assertAlmostEqual(flows[0]["share"], 0.9)
        self.assertEqual(flows[0]["wallets"], 3)
        self.assertFalse(flows[0]["urgent"])

    def test_one_large_wallet_does_not_trigger_market_flow(self):
        now = 1800000000
        entries = [(now - ix, "Team B", "whale", 3000, 0.5) for ix in range(3)]
        self.assertEqual(assess_market_flow(entries, ["Team A", "Team B"], now), [])

    def test_no_synthetic_burst_without_sufficient_history(self):
        now = 1800000000
        found = assess_market_flow(flow_rows(now), ["Team A", "Team B"], now)
        self.assertIsNone(found[0]["burst_ratio"])
        self.assertIsNone(found[0]["delta_pp"])

    def test_burst_relative_to_prior_345_minutes(self):
        now = 1800000000
        recent = flow_rows(now, side_a=800, side_b=3600)
        old = [(now - 3600, "Team A", "old", 500, .5),
               (now - 3300, "Team B", "old2", 500, .5)]
        found = assess_market_flow(recent + old, ["Team A", "Team B"], now)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["burst"])
        self.assertGreater(found[0]["burst_ratio"], 4)
        self.assertGreater(found[0]["delta_pp"], 0)

    def test_non_binary_market_not_mixed(self):
        now = 1800000000
        self.assertEqual(assess_market_flow(flow_rows(now), ["Team A", "Team B", "Draw"], now), [])


class MarketFlowStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(str(Path(tmp.name) / "test.sqlite3"))
        self.addCleanup(self.db.conn.close)
        self.cfg = DEFAULT.copy()
        self.cfg.update({"wallet_history_check": False, "flow_enabled": True})
        self.client = type("TestClient", (), {"messages": [], "post_telegram":
            lambda c, token, chat_id, message: c.messages.append(message)})()
        self.radar = Radar(self.client, self.db, self.cfg, "token", "chat", False)
        self.radar.markets = {
            COND1: EventMarket(COND1, "99", "Team A vs Team B", "Map 2 Winner",
                               "map-2-test", 10000, None, "lower", ["Team A", "Team B"]),
            COND2: EventMarket(COND2, "99", "Team A vs Team B", "Match Winner",
                               "match-test", 10000, None, "lower", ["Team A", "Team B"]),
        }

    def test_digest_uses_unique_fills_not_cumulative_position(self):
        now = int(time.time())
        trade = tx(COND1, "wallet-1", 600, now - 20, "id-1")
        self.assertTrue(self.db.add_trade(trade))
        self.assertFalse(self.db.add_trade(trade))
        self.assertTrue(self.db.queue_normal_signal(
            trade, "position", now, 1200, total_usd=9600, avg_odds=2.0))
        self.assertTrue(self.radar.normal_digest(now))
        self.radar.flush_outbox()
        text = self.client.messages[0]
        self.assertIn("Map 2 Winner", text)
        self.assertIn("600 USD", text)
        self.assertNotIn("9,600 USD", text)
        self.assertIn("1 zakupów", text)

    def test_separate_markets_not_mixed(self):
        now = int(time.time())
        self.db.add_trade(tx(COND1, "wallet-1", 500, now, "id-1"))
        self.db.add_trade(tx(COND2, "wallet-2", 1000, now, "id-2"))
        n1 = self.db.observed_buy_summary(COND1, "Team B", now - 60, now + 1)
        n2 = self.db.observed_buy_summary(COND2, "Team B", now - 60, now + 1)
        self.assertEqual(n1[1], 500)
        self.assertEqual(n2[1], 1000)

    def test_urgent_dedup_works_after_priority_trade(self):
        now = int(time.time())
        k = f"priority:{COND1}:wallet-1:Team B"
        self.db.queue_alert(k, now, "alert", [])
        self.assertTrue(self.db.recently_alerted_market(COND1, "Team B", now))
        self.assertFalse(self.db.recently_alerted_market(COND2, "Team B", now))

    def test_flow_signal_in_digest_does_not_overcount_with_normal(self):
        now = int(time.time())
        for ix, row in enumerate(flow_rows(now)):
            timestamp, side, wallet, amount, price = row
            trade = tx(COND1, wallet, amount, timestamp, f"id-{ix}", side)
            self.db.add_trade(trade)
            if ix == 0:
                self.db.queue_normal_signal(trade, "single", now, 1800,
                                            total_usd=3500, avg_odds=2.0)
        self.assertTrue(self.db.queue_market_flow_signal(COND1, "Team B", now, 3600, 2.0))
        self.assertTrue(self.radar.normal_digest(now))
        self.radar.flush_outbox()
        message = self.client.messages[0]
        self.assertEqual(message.count("📊 PRZECHYLENIE:"), 1)
        self.assertIn("2,700 USD", message)
        self.assertNotIn("6,200 USD", message)
        self.assertIn("Map 2 Winner", message)

    def test_market_flow_alerts_once_per_touched_market(self):
        now = int(time.time())
        self.cfg.update({"flow_priority_cooldown_seconds": 3600})
        for ix, row in enumerate(flow_rows(now)):
            timestamp, side, wallet, amount, price = row
            self.db.add_trade(tx(COND1, wallet, amount, timestamp, f"flow-{ix}", side))
        self.radar.flow_conditions.add(COND1)
        self.radar.market_flow_alerts(now)
        self.assertEqual(len(self.db.pending_normal_signals()), 1)
        self.radar.flow_conditions.add(COND1)
        self.radar.market_flow_alerts(now + 5)
        self.assertEqual(len(self.db.pending_normal_signals()), 1)


if __name__ == "__main__":
    unittest.main()
