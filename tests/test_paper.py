"""Synthetic broker checks; no native imports or network connections."""
import unittest

from wangshu_lite.models import OrderIntent
from wangshu_lite.paper import PaperGateway, SYMBOL


class MemoryJournal:
    def __init__(self):
        self.values = {}
        self.fail = False

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        if self.fail:
            raise OSError("synthetic disk failure")
        self.values[key] = value


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.journal = MemoryJournal()
        self.gateway = PaperGateway(lambda *event: self.events.append(event), self.journal)
        self.gateway.connect()

    def insert(self, key="request", side="BUY", offset="OPEN", volume=3, price=3502, kind="LIMIT"):
        return self.gateway.send_order(OrderIntent(key, SYMBOL, side, offset, volume, price, kind), None)

    def test_partial_fill_cancel_restart_preserves_broker_facts_and_ids(self):
        oid = self.insert()
        self.gateway.fill(oid, 1)
        self.assertEqual(self.gateway.orders[oid].status, "PARTIAL")
        self.gateway.cancel_order(self.gateway.orders[oid])
        self.assertEqual((self.gateway.orders[oid].status, self.gateway.orders[oid].traded), ("CANCELLED", 1))
        self.gateway.close()
        self.gateway = PaperGateway(lambda *event: self.events.append(event), self.journal)
        self.gateway.connect()
        self.assertEqual(self.gateway.positions[0].volume, 1)
        self.assertEqual(self.gateway.orders[oid].status, "CANCELLED")
        close = self.insert("close", "SELL", "CLOSE_TODAY", volume=1)
        self.gateway.fill(close)
        self.assertFalse(self.gateway.positions)
        self.assertEqual(len(self.gateway.trades), 2)
        self.assertNotEqual(oid, close)

    def test_working_order_survives_restart_without_inventing_cancellation(self):
        oid = self.insert()
        restored = PaperGateway(lambda *_event: None, self.journal)
        restored.connect()
        self.assertTrue(restored.orders[oid].active)
        self.assertEqual(restored.orders[oid].traded, 0)

    def test_fak_partial_and_fok_all_or_none_at_explicit_two_lot_depth(self):
        fak = self.insert(kind="FAK", volume=3)
        fok = self.insert("fok", kind="FOK", volume=3)
        filled = self.insert("filled", kind="FOK", volume=2)
        self.assertEqual((self.gateway.orders[fak].status, self.gateway.orders[fak].traded), ("CANCELLED", 2))
        self.assertEqual((self.gateway.orders[fok].status, self.gateway.orders[fok].traded), ("CANCELLED", 0))
        self.assertEqual((self.gateway.orders[filled].status, self.gateway.orders[filled].traded), ("FILLED", 2))

    def test_failed_broker_commit_does_not_publish_or_keep_phantom_fill(self):
        oid = self.insert()
        count = len(self.events)
        self.journal.fail = True
        with self.assertRaises(OSError):
            self.gateway.fill(oid, 1)
        self.assertEqual(len(self.events), count)
        self.assertEqual(self.gateway.orders[oid].traded, 0)
        self.assertFalse(self.gateway.positions)
        self.assertFalse(self.gateway.trades)
        self.assertEqual(self.gateway.trade_sequence, 0)

    def test_quotes_include_cumulative_volume_for_vnpy_bar_generator(self):
        self.gateway.pulse()
        first = self.gateway.quote(SYMBOL)
        self.gateway.pulse()
        second = self.gateway.quote(SYMBOL)
        self.assertGreater(second.volume, first.volume)
        self.assertGreater(second.turnover, first.turnover)
        self.assertGreater(second.open_interest, 0)


if __name__ == "__main__":
    unittest.main()
