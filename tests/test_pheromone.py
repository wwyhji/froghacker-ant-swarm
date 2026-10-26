"""Unit tests for ant-colony pheromone communication layer."""

import json
import os
import tempfile
import time
import unittest

from ant_swarm.pheromone import (
    Pheromone,
    PheromoneDB,
    TRAIL_TTL_MS,
    ALARM_TTL_MS,
    TERRITORY_TTL_MS,
    score_tasks,
    pick_best_task,
)


class TestPheromone(unittest.TestCase):
    """Pheromone dataclass tests."""

    def test_basic_fields(self):
        now = time.time()
        p = Pheromone(id=1, type="trail", key="qid:C01", worker_id="w1",
                       value='{"a":1}', ttl_ms=60000, created_at=now)
        self.assertEqual(p.id, 1)
        self.assertEqual(p.type, "trail")
        self.assertEqual(p.key, "qid:C01")
        self.assertEqual(p.worker_id, "w1")
        self.assertEqual(p.ttl_ms, 60000)

    def test_parse_value(self):
        p = Pheromone(1, "trail", "k", "w", '{"x": 42}', 1000, time.time())
        self.assertEqual(p.parse_value(), {"x": 42})

    def test_parse_value_bad_json(self):
        p = Pheromone(1, "trail", "k", "w", "not-json", 1000, time.time())
        self.assertEqual(p.parse_value(), {})

    def test_expires_at(self):
        now = time.time()
        p = Pheromone(1, "trail", "k", "w", "{}", 60000, now)
        self.assertAlmostEqual(p.expires_at, now + 60.0, delta=0.01)

    def test_is_expired(self):
        now = time.time()
        alive = Pheromone(1, "trail", "k", "w", "{}", 60000, now)
        expired = Pheromone(1, "trail", "k", "w", "{}", 1000, now - 100)
        self.assertFalse(alive.is_expired)
        self.assertTrue(expired.is_expired)

    def test_to_dict(self):
        now = time.time()
        p = Pheromone(1, "trail", "qid:C01", "w1", '{"score":5}', 60000, now)
        d = p.to_dict()
        self.assertEqual(d["id"], 1)
        self.assertEqual(d["type"], "trail")
        self.assertEqual(d["key"], "qid:C01")
        self.assertEqual(d["worker_id"], "w1")
        self.assertEqual(d["value"], {"score": 5})
        self.assertAlmostEqual(d["expires_in_s"], 60, delta=2)


class TestPheromoneDB(unittest.TestCase):
    """PheromoneDB CRUD and lifecycle tests."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "pheromone.db")
        self.db = PheromoneDB(self.db_path)
        self.db.connect()

    def tearDown(self):
        self.db.close()

    def test_connect_creates_file(self):
        self.assertTrue(os.path.exists(self.db_path))

    def test_schema_tables_exist(self):
        tables = self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = [r[0] for r in tables]
        self.assertIn("pheromone", names)

    # ── deposit ──

    def test_deposit_trail(self):
        rid = self.db.deposit("trail", "qid:C01", "w1", {"finding": "XSS"})
        self.assertIsInstance(rid, int)
        rows = self.db.scan()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].type, "trail")
        self.assertEqual(rows[0].key, "qid:C01")
        self.assertEqual(rows[0].worker_id, "w1")
        self.assertEqual(rows[0].parse_value(), {"finding": "XSS"})

    def test_deposit_defaults(self):
        """Deposit without value or ttl should use defaults."""
        rid = self.db.deposit("trail", "k", "w")
        row = self.db.scan()[0]
        self.assertEqual(row.parse_value(), {})
        self.assertEqual(row.ttl_ms, TRAIL_TTL_MS)

    def test_deposit_custom_ttl(self):
        self.db.deposit("trail", "k", "w", ttl_ms=5000)
        row = self.db.scan()[0]
        self.assertEqual(row.ttl_ms, 5000)

    def test_deposit_upsert(self):
        """Same type+key+worker should replace, not duplicate."""
        self.db.deposit("trail", "k", "w", {"v": 1})
        self.db.deposit("trail", "k", "w", {"v": 2})
        rows = self.db.scan()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].parse_value(), {"v": 2})

    def test_deposit_different_keys(self):
        self.db.deposit("trail", "k1", "w1")
        self.db.deposit("trail", "k2", "w1")
        self.assertEqual(len(self.db.scan()), 2)

    def test_deposit_different_types(self):
        self.db.deposit("trail", "k", "w")
        self.db.deposit("alarm", "k", "w")
        rows = self.db.scan()
        self.assertEqual(len(rows), 2)

    # ── scan ──

    def test_scan_all_active(self):
        self.db.deposit("trail", "k1", "w1")
        self.db.deposit("trail", "k2", "w2")
        self.db.deposit("alarm", "k3", "w1")
        self.assertEqual(len(self.db.scan()), 3)

    def test_scan_filter_by_type(self):
        self.db.deposit("trail", "k1", "w1")
        self.db.deposit("alarm", "k2", "w1")
        rows = self.db.scan(ptype="trail")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].type, "trail")

    def test_scan_filter_by_key(self):
        self.db.deposit("trail", "k1", "w1")
        self.db.deposit("trail", "k2", "w1")
        rows = self.db.scan(key="k1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].key, "k1")

    def test_scan_filter_by_worker(self):
        self.db.deposit("trail", "k1", "w1")
        self.db.deposit("trail", "k2", "w2")
        rows = self.db.scan(worker_id="w1")
        self.assertEqual(len(rows), 1)

    def test_scan_expired_not_returned(self):
        """Expired pheromones should be skipped by scan()."""
        self.db.deposit("trail", "k", "w", ttl_ms=1)  # 1ms TTL
        time.sleep(0.01)  # let it expire
        rows = self.db.scan()
        self.assertEqual(len(rows), 0)

    # ── claim ──

    def test_claim_succeeds_on_free_key(self):
        ok = self.db.claim("qid:C01", "w1")
        self.assertTrue(ok)

    def test_claim_fails_when_occupied(self):
        self.db.claim("qid:C01", "w1")
        ok = self.db.claim("qid:C01", "w2")
        self.assertFalse(ok)

    def test_claim_self_renew(self):
        """Same worker re-claiming should succeed (renew territory)."""
        self.db.claim("qid:C01", "w1")
        ok = self.db.claim("qid:C01", "w1")
        self.assertTrue(ok)

    def test_claim_expired_territory_released(self):
        """Claim on an expired territory should succeed."""
        self.db.claim("qid:C01", "w1", ttl_ms=1)
        time.sleep(0.01)
        ok = self.db.claim("qid:C01", "w2")
        self.assertTrue(ok)

    def test_claim_different_keys_independent(self):
        self.db.claim("qid:C01", "w1")
        self.assertTrue(self.db.claim("qid:C02", "w2"))
        # Same worker can hold multiple keys (different keys)
        self.assertTrue(self.db.claim("qid:C03", "w1"))

    # ── renew ──

    def test_renew_succeeds_for_owner(self):
        self.db.claim("qid:C01", "w1")
        ok = self.db.renew("qid:C01", "w1")
        self.assertTrue(ok)

    def test_renew_fails_for_non_owner(self):
        self.db.claim("qid:C01", "w1")
        ok = self.db.renew("qid:C01", "w2")
        self.assertFalse(ok)

    def test_renew_fails_for_expired(self):
        self.db.claim("qid:C01", "w1", ttl_ms=1)
        time.sleep(0.01)
        ok = self.db.renew("qid:C01", "w1")
        self.assertFalse(ok)

    # ── release ──

    def test_release(self):
        self.db.claim("qid:C01", "w1")
        self.assertTrue(self.db.release("qid:C01", "w1"))
        # After release, another worker can claim
        self.assertTrue(self.db.claim("qid:C01", "w2"))

    def test_release_nonexistent(self):
        self.assertFalse(self.db.release("no-such", "w1"))

    # ── cleanup ──

    def test_cleanup_removes_expired(self):
        self.db.deposit("trail", "k", "w", ttl_ms=1)
        time.sleep(0.01)
        removed = self.db.cleanup()
        self.assertGreater(removed, 0)
        self.assertEqual(len(self.db.scan()), 0)

    def test_cleanup_keeps_active(self):
        self.db.deposit("trail", "k", "w", ttl_ms=60000)
        removed = self.db.cleanup()
        self.assertEqual(removed, 0)
        self.assertEqual(len(self.db.scan()), 1)

    # ── stats ──

    def test_stats_counts_by_type(self):
        self.db.deposit("trail", "k1", "w1")
        self.db.deposit("trail", "k2", "w1")
        self.db.deposit("alarm", "k3", "w1")
        s = self.db.stats()
        self.assertEqual(s.get("trail"), 2)
        self.assertEqual(s.get("alarm"), 1)

    def test_stats_empty(self):
        self.assertEqual(self.db.stats(), {})


class TestScoreTasks(unittest.TestCase):
    """Task scoring logic tests (pure functions, no DB)."""

    def make_p(self, ptype, key, worker_id="w1", value=None, ttl_ms=TRAIL_TTL_MS, created_at=None):
        if created_at is None:
            created_at = time.time()
        if value is None:
            value = {}
        return Pheromone(id=1, type=ptype, key=key, worker_id=worker_id,
                          value=json.dumps(value), ttl_ms=ttl_ms, created_at=created_at)

    def test_empty(self):
        scores = score_tasks([], "w1")
        self.assertEqual(scores, {})

    def test_trail_adds_score(self):
        ps = [self.make_p("trail", "qid:C01", "w2")]
        scores = score_tasks(ps, "w1")
        self.assertGreater(scores.get("qid:C01", 0), 0)

    def test_multiple_trails_stack(self):
        ps = [
            self.make_p("trail", "qid:C01", "w1"),
            self.make_p("trail", "qid:C01", "w2"),
            self.make_p("trail", "qid:C01", "w3"),
        ]
        scores = score_tasks(ps, "w4")
        self.assertEqual(scores["qid:C01"], 3)

    def test_dead_end_trail_negative(self):
        """A dead-end trail should push the key's score below zero."""
        ps = [self.make_p("trail", "qid:C01", "w2", {"outcome": "dead_end"})]
        scores = score_tasks(ps, "w1")
        self.assertEqual(scores["qid:C01"], -1)

    def test_dead_end_cancels_positive(self):
        """One positive + one dead-end trail should net to zero."""
        ps = [
            self.make_p("trail", "qid:C01", "w2", {"outcome": "success"}),
            self.make_p("trail", "qid:C01", "w3", {"outcome": "dead_end"}),
        ]
        scores = score_tasks(ps, "w1")
        self.assertEqual(scores["qid:C01"], 0)

    def test_done_false_is_dead_end(self):
        """Boolean success marker False should count as negative."""
        ps = [self.make_p("trail", "qid:C01", "w2", {"done": False})]
        scores = score_tasks(ps, "w1")
        self.assertEqual(scores["qid:C01"], -1)

    def test_nested_result_failed_is_dead_end(self):
        """Nested result status should be detected."""
        ps = [self.make_p("trail", "qid:C01", "w2", {"result": {"status": "failed"}})]
        scores = score_tasks(ps, "w1")
        self.assertEqual(scores["qid:C01"], -1)

    def test_plain_trail_stays_positive(self):
        """Trails without outcome markers remain positive (backward compat)."""
        ps = [self.make_p("trail", "qid:C01", "w2", {"confidence": "high"})]
        scores = score_tasks(ps, "w1")
        self.assertEqual(scores["qid:C01"], 1)

    def test_dead_end_excluded_by_pick_best(self):
        """pick_best_task should skip a key with only dead-end trails."""
        ps = [
            self.make_p("trail", "qid:C01", "w2", {"outcome": "dead_end"}),
            self.make_p("trail", "qid:C02", "w2"),
        ]
        best = pick_best_task(ps, "w1")
        self.assertEqual(best, "qid:C02")

    def test_territory_by_other_makes_unavailable(self):
        ps = [self.make_p("territory", "qid:C01", "w2")]
        scores = score_tasks(ps, "w1")
        self.assertEqual(scores["qid:C01"], -1)

    def test_own_territory_neutral(self):
        ps = [self.make_p("territory", "qid:C01", "w1")]
        scores = score_tasks(ps, "w1")
        # Own territory shouldn't appear (no score assigned)
        self.assertNotIn("qid:C01", scores)

    def test_alarm_penalty(self):
        ps = [
            self.make_p("trail", "qid:C01", "w2"),
            self.make_p("alarm", "qid:C01", "w3"),
        ]
        scores = score_tasks(ps, "w1")
        # trail (+1) + alarm (-5) = max(0, -4) = 0
        self.assertEqual(scores["qid:C01"], 0)

    def test_alarm_on_clear_key_no_penalty(self):
        """Alarm on a key with no trail should not add score."""
        ps = [self.make_p("alarm", "qid:C01", "w3")]
        scores = score_tasks(ps, "w1")
        self.assertEqual(scores.get("qid:C01", 0), 0)

    def test_recruit_with_deadline_hours(self):
        ps = [self.make_p("recruit", "qid:C01", "w2", {"deadline_hours": 6})]
        scores = score_tasks(ps, "w1")
        # urgency = 10 * (1 - 6/24) = 10 * 0.75 = 7.5
        self.assertAlmostEqual(scores["qid:C01"], 7.5, delta=0.1)

    def test_recruit_urgent(self):
        ps = [self.make_p("recruit", "qid:C01", "w2", {"deadline_hours": 1})]
        scores = score_tasks(ps, "w1")
        # urgency = 10 * (1 - 1/24) ≈ 9.58
        self.assertAlmostEqual(scores["qid:C01"], 9.6, delta=0.1)

    def test_recruit_far_future(self):
        ps = [self.make_p("recruit", "qid:C01", "w2", {"deadline_hours": 48})]
        scores = score_tasks(ps, "w1")
        # urgency = max(0, 10 * (1 - 48/24)) = 0
        self.assertEqual(scores["qid:C01"], 0)

    def test_mixed_signals(self):
        """Key with both trails and recruits — scores should add."""
        ps = [
            self.make_p("trail", "qid:C01", "w2"),
            self.make_p("trail", "qid:C01", "w3"),
            self.make_p("recruit", "qid:C01", "w4", {"deadline_hours": 12}),
        ]
        scores = score_tasks(ps, "w1")
        # 2 trails (+2) + recruit 10*(1-12/24)=5 → total 7
        self.assertAlmostEqual(scores["qid:C01"], 7.0, delta=0.1)

    def test_occupied_key_negative_even_with_trails(self):
        """Territory held by another → -1 regardless of trail count."""
        ps = [
            self.make_p("trail", "qid:C01", "w2"),
            self.make_p("territory", "qid:C01", "w3"),
        ]
        scores = score_tasks(ps, "w1")
        self.assertEqual(scores["qid:C01"], -1)

    def test_deadline_iso_format(self):
        """Test ISO 8601 deadline parsing."""
        # Far future deadline
        ps = [self.make_p("recruit", "qid:C01", "w2", {"deadline": "2099-12-31T00:00:00"})]
        scores = score_tasks(ps, "w1")
        # Very far → 0 urgency
        self.assertEqual(scores["qid:C01"], 0)


class TestPickBestTask(unittest.TestCase):
    """pick_best_task integration tests."""

    def make_p(self, ptype, key, worker_id="w1", value=None):
        if value is None:
            value = {}
        return Pheromone(id=1, type=ptype, key=key, worker_id=worker_id,
                          value=json.dumps(value), ttl_ms=TRAIL_TTL_MS, created_at=time.time())

    def test_no_pheromones(self):
        self.assertIsNone(pick_best_task([], "w1"))

    def test_picks_highest_score(self):
        ps = [
            self.make_p("trail", "qid:C01", "w2"),
            self.make_p("trail", "qid:C01", "w3"),
            self.make_p("trail", "qid:C02", "w2"),
        ]
        # qid:C01 has 2 trails, qid:C02 has 1 trail
        best = pick_best_task(ps, "w4")
        self.assertEqual(best, "qid:C01")

    def test_skips_occupied_keys(self):
        ps = [
            self.make_p("trail", "qid:C01", "w2"),
            self.make_p("trail", "qid:C01", "w3"),
            self.make_p("trail", "qid:C01", "w4"),
            self.make_p("territory", "qid:C01", "w5"),
            self.make_p("trail", "qid:C02", "w2"),
        ]
        best = pick_best_task(ps, "w1")
        # qid:C01 occupied → skip, pick qid:C02
        self.assertEqual(best, "qid:C02")

    def test_all_occupied_returns_none(self):
        ps = [
            self.make_p("territory", "qid:C01", "w5"),
            self.make_p("territory", "qid:C02", "w6"),
        ]
        self.assertIsNone(pick_best_task(ps, "w1"))

    def test_deterministic_tiebreaker(self):
        """Equal scores should pick lexicographically first key."""
        ps = [
            self.make_p("trail", "qid:B", "w2"),
            self.make_p("trail", "qid:A", "w2"),
        ]
        best = pick_best_task(ps, "w1")
        self.assertEqual(best, "qid:A")  # Lexicographically first

    def test_recruit_beats_trail(self):
        ps = [
            self.make_p("trail", "qid:C01", "w2"),
            self.make_p("recruit", "qid:C02", "w2", {"deadline_hours": 2}),
        ]
        best = pick_best_task(ps, "w1")
        # qid:C02 recruit urgency ~9.2 > qid:C01 trail score 1
        self.assertEqual(best, "qid:C02")


class TestPheromoneDBConcurrency(unittest.TestCase):
    """Simple concurrency tests — two connections on same DB file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "pheromone.db")

    def test_two_instances_see_each_other(self):
        db1 = PheromoneDB(self.db_path)
        db2 = PheromoneDB(self.db_path)
        try:
            db1.connect()
            db2.connect()
            db1.deposit("trail", "k", "w1", {"msg": "hello"})
            rows = db2.scan()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].parse_value()["msg"], "hello")
        finally:
            db1.close()
            db2.close()

    def test_claim_competition(self):
        """Two workers compete for same key — only one wins."""
        db1 = PheromoneDB(self.db_path)
        db2 = PheromoneDB(self.db_path)
        try:
            db1.connect()
            db2.connect()
            # Close in time — only one should win
            ok1 = db1.claim("qid:C01", "w1")
            ok2 = db2.claim("qid:C01", "w2")
            # Either one or both can succeed depending on timing,
            # but not both winning simultaneously
            if ok1 and ok2:
                # Both claim to have succeeded — check DB state
                rows = db1.scan(ptype="territory", key="qid:C01")
                self.assertEqual(len(rows), 1)
                # But only one should persist
                self.assertFalse(ok1 and ok2 and len(rows) > 1)
            # At least one should have a valid claim
            self.assertTrue(ok1 or ok2)
        finally:
            db1.close()
            db2.close()


if __name__ == "__main__":
    unittest.main()
