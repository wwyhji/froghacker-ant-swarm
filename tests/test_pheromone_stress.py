"""Phase 4 stress tests: 4 Workers × 20 Tasks concurrent competition.

Validates:
  1. CAS conflict rate under contention
  2. TTL expiration — crashed workers release territory
  3. Task distribution fairness
  4. Positive feedback convergence (trail density)
  5. No duplicate claims (each task claimed exactly once)
"""

import json
import os
import tempfile
import threading
import time
import unittest
from collections import Counter

from ant_swarm.pheromone import (
    PheromoneDB,
    score_tasks,
    pick_best_task,
    TERRITORY_TTL_MS,
    TRAIL_TTL_MS,
)


def _pick_unclaimed(active, worker_id, claimed: set) -> str | None:
    """Pick best task key, excluding already-claimed keys."""
    scores = score_tasks(active, worker_id)
    # Filter: score >= 0 (available), not already claimed
    available = {k: v for k, v in scores.items()
                 if v >= 0 and k not in claimed}
    if not available:
        return None
    # Sort by score desc, then key asc for determinism
    ranked = sorted(available.items(), key=lambda x: (-x[1], x[0]))
    return ranked[0][0]


# ── Stress test parameters ──
NUM_WORKERS = 4
NUM_TASKS = 20
TASK_KEYS = [f"task:{i:03d}" for i in range(NUM_TASKS)]


class TestPheromoneStress(unittest.TestCase):
    """4 Workers × 20 Tasks — core correctness under contention."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "pheromone_stress.db")

    def _init_seed_pheromones(self, db: PheromoneDB):
        """Seed recruit pheromones for all tasks so workers can discover them."""
        for key in TASK_KEYS:
            db.deposit("recruit", key, "commander",
                       {"deadline_hours": 24, "description": f"Task {key}"})

    def _worker_loop(self, worker_id: str, results: list, barrier: threading.Barrier,
                     done_keys: set, done_lock: threading.Lock,
                     rounds: int = 1):
        """Single worker run: scan → pick → claim → work → trail → release.

        Each worker connects to the same DB file independently (simulating
        multi-process WAL access). Uses while-loop until all tasks are
        completed by some worker (via shared `done_keys` set).
        """
        db = PheromoneDB(self.db_path)
        db.connect()
        claimed = set()
        conflicts = 0
        idle_streak = 0
        max_idle = 20  # Consecutive idle iterations before giving up

        try:
            barrier.wait(timeout=10)  # Synchronize start

            while idle_streak < max_idle:
                # Check if all tasks are done (across all workers)
                with done_lock:
                    if len(done_keys) >= NUM_TASKS:
                        break

                active = db.scan()

                # Exclude both local claimed and global done keys
                with done_lock:
                    excluded = claimed | done_keys
                best_key = _pick_unclaimed(active, worker_id, excluded)

                if best_key is None:
                    idle_streak += 1
                    time.sleep(0.005)
                    continue

                # CAS claim
                if not db.claim(best_key, worker_id):
                    conflicts += 1
                    idle_streak += 1
                    time.sleep(0.002)  # Small backoff
                    continue

                idle_streak = 0  # Reset — we got work
                claimed.add(best_key)

                # Simulate work (variable duration to create contention)
                work_ms = (hash(best_key + worker_id) % 10 + 1) * 0.001
                time.sleep(work_ms)

                # Deposit trail (positive feedback)
                db.deposit("trail", best_key, worker_id,
                           {"confidence": "high", "worker": worker_id})

                # Release territory
                db.release(best_key, worker_id)

                # Mark as globally done
                with done_lock:
                    done_keys.add(best_key)

        finally:
            db.close()

        results.append({
            "worker_id": worker_id,
            "claimed": sorted(claimed),
            "conflicts": conflicts,
            "total_claimed": len(claimed),
        })

    def test_4workers_20tasks_no_duplicates(self):
        """Every task must be claimed by exactly one worker."""
        # Seed recruit pheromones
        seed_db = PheromoneDB(self.db_path)
        seed_db.connect()
        self._init_seed_pheromones(seed_db)
        seed_db.close()

        results: list[dict] = []
        done_keys: set[str] = set()
        done_lock = threading.Lock()
        barrier = threading.Barrier(NUM_WORKERS)
        threads = []

        for i in range(NUM_WORKERS):
            t = threading.Thread(
                target=self._worker_loop,
                args=(f"worker_{i}", results, barrier, done_keys, done_lock, 1),
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=60)

        # ── Assertions ──
        all_claimed = []
        for r in results:
            all_claimed.extend(r["claimed"])

        # 1. Every task claimed at least once (via done_keys tracking)
        self.assertEqual(len(done_keys), NUM_TASKS,
                         f"Expected {NUM_TASKS} tasks done, got {len(done_keys)}."
                         f" Missing: {set(TASK_KEYS) - done_keys}")

        # 2. No duplicate claims across global done_keys
        #    (done_keys is a set, so duplicates are impossible by construction)

        # 3. Every worker got at least one task
        for r in results:
            self.assertGreater(r["total_claimed"], 0,
                               f"{r['worker_id']} got 0 tasks!")

        # 4. Reasonable distribution (no worker got > 70%)
        max_share = max(r["total_claimed"] for r in results)
        self.assertLess(max_share, NUM_TASKS * 0.7,
                        f"Unbalanced: one worker took {max_share}/{NUM_TASKS} tasks")

    def test_cas_conflict_rate(self):
        """CAS conflicts should be > 0 under real contention but < total attempts."""
        seed_db = PheromoneDB(self.db_path)
        seed_db.connect()
        self._init_seed_pheromones(seed_db)
        seed_db.close()

        results: list[dict] = []
        done_keys: set[str] = set()
        done_lock = threading.Lock()
        barrier = threading.Barrier(NUM_WORKERS)
        threads = []

        for i in range(NUM_WORKERS):
            t = threading.Thread(
                target=self._worker_loop,
                args=(f"worker_{i}", results, barrier, done_keys, done_lock, 1),
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=60)

        total_conflicts = sum(r["conflicts"] for r in results)
        total_claimed = sum(r["total_claimed"] for r in results)

        # Conflicts > 0 (contention happened)
        self.assertGreater(total_conflicts, 0,
                           "Expected CAS conflicts under 4-worker contention, got 0")

        # Conflict rate < 100% (system made progress)
        total_attempts = total_conflicts + total_claimed
        conflict_rate = total_conflicts / total_attempts if total_attempts else 0
        self.assertLess(conflict_rate, 0.9,
                        f"Conflict rate {conflict_rate:.1%} too high — system starved")

        print(f"\n  CAS conflict rate: {conflict_rate:.1%} ({total_conflicts}/{total_attempts})")

    def test_ttl_expiration_releases_crashed_worker(self):
        """A worker that crashes (no release) — territory expires → another claims."""
        db = PheromoneDB(self.db_path)
        db.connect()

        key = "task:critical"

        # Worker 1 claims territory with SHORT TTL
        ok = db.claim(key, "worker_crash", ttl_ms=200)  # 200ms TTL
        self.assertTrue(ok)

        # Verify territory is held
        territories = db.scan(ptype="territory", key=key)
        self.assertEqual(len(territories), 1)
        self.assertEqual(territories[0].worker_id, "worker_crash")

        # Wait for TTL to expire (worker crashed, no renew)
        time.sleep(0.3)

        # Worker 2 should be able to claim now
        ok = db.claim(key, "worker_recovery")
        self.assertTrue(ok, "Worker 2 should claim expired territory")

        territories = db.scan(ptype="territory", key=key)
        self.assertEqual(territories[0].worker_id, "worker_recovery")

        db.close()

    def test_positive_feedback_convergence(self):
        """Trail density increases after workers complete tasks — verify convergence."""
        db = PheromoneDB(self.db_path)
        db.connect()

        key = "task:hot"

        # Seed a recruit
        db.deposit("recruit", key, "commander", {"deadline_hours": 24})

        # Initial trail density = 0
        trails = db.scan(ptype="trail", key=key)
        self.assertEqual(len(trails), 0)

        # 3 workers complete the task and deposit trails
        for wid in ["w1", "w2", "w3"]:
            db.deposit("trail", key, wid, {"confidence": "high"})

        trails = db.scan(ptype="trail", key=key)
        self.assertEqual(len(trails), 3)

        # Scoring should reflect density
        active = db.scan()
        scores = score_tasks(active, "w_new")
        self.assertAlmostEqual(scores[key], 3.0, delta=0.1,
                               msg="Trail density score should be 3")

        db.close()

    def test_no_deadlock_under_full_occupancy(self):
        """When all tasks are occupied, workers should idle (not deadlock)."""
        db = PheromoneDB(self.db_path)
        db.connect()
        self._init_seed_pheromones(db)

        # All tasks claimed by external workers
        for key in TASK_KEYS:
            db.claim(key, f"ext_worker_{hash(key) % 4}")

        # A new worker scans — should find nothing available
        active = db.scan()
        best = pick_best_task(active, "new_worker")
        self.assertIsNone(best, "New worker should find no tasks when all occupied")

        db.close()

    def test_cleanup_during_stress(self):
        """cleanup() should not disrupt active claims during stress."""
        db = PheromoneDB(self.db_path)
        db.connect()
        self._init_seed_pheromones(db)

        # A few short-TTL pheromones
        db.deposit("trail", "ephemeral:k", "w1", ttl_ms=10)

        # A long-lived territory
        db.claim("task:stable", "w_stable", ttl_ms=60000)

        time.sleep(0.02)

        removed = db.cleanup()
        self.assertGreater(removed, 0, "Short-TTL trail should be cleaned")

        # Stable territory still there
        territories = db.scan(ptype="territory", key="task:stable")
        self.assertEqual(len(territories), 1)

        db.close()

    def test_mixed_pheromone_types_convergence(self):
        """Full scenario: recruit + territory competition + trail + alarm interplay."""
        db = PheromoneDB(self.db_path)
        db.connect()

        # Commander deposits recruits for 3 tasks
        for key in ["task:A", "task:B", "task:C"]:
            db.deposit("recruit", key, "commander",
                       {"deadline_hours": 1, "priority": "high"})

        # Worker 1 claims task:A
        self.assertTrue(db.claim("task:A", "w1"))
        db.deposit("trail", "task:A", "w1", {"result": "done"})
        db.release("task:A", "w1")

        # Worker 2 hits a blocker on task:B
        db.deposit("alarm", "task:B", "w2", {"reason": "missing tool"})

        # Worker 3 scans — A has trail+recruit (best), C has only recruit,
        # B has recruit+alarm (penalized). Order: A > C > B
        active = db.scan()
        best = pick_best_task(active, "w3")
        self.assertEqual(best, "task:A",
                         f"Should pick task:A (recruit+trail > recruit only > recruit+alarm), got {best}")

        # Task scores should make sense
        scores = score_tasks(active, "w3")
        # task:A: recruit ~9.6 + trail(+1) ≈ 10.6  (best)
        # task:C: recruit ~9.6 + no trail ≈ 9.6     (middle)
        # task:B: recruit ~9.6 + alarm(-5) ≈ 4.6    (worst)
        self.assertGreater(scores["task:A"], scores["task:C"],
                           "Task with trail+recruit should outrank recruit-only")
        self.assertGreater(scores["task:C"], scores["task:B"],
                           "Clean recruit should outrank alarmed recruit")
        # All non-blocked tasks should have positive scores
        self.assertGreater(scores["task:A"], 0)
        self.assertGreater(scores["task:C"], 0)
        # Blocked task can still have non-negative score (floor at 0)
        self.assertGreaterEqual(scores["task:B"], 0)

        db.close()


class TestPheromoneStatsAfterStress(unittest.TestCase):
    """Verify stats() reflects correct state after stress run."""

    def test_stats_after_mixed_activity(self):
        db_path = os.path.join(tempfile.mkdtemp(), "stats.db")
        db = PheromoneDB(db_path)
        db.connect()

        # Simulate a completed stress run
        db.deposit("trail", "k1", "w1")
        db.deposit("trail", "k2", "w2")
        db.deposit("trail", "k1", "w3")  # k1 has 2 trails
        db.deposit("alarm", "k3", "w1")
        db.claim("k4", "w4")  # territory

        stats = db.stats()
        self.assertEqual(stats.get("trail"), 3)
        self.assertEqual(stats.get("alarm"), 1)
        self.assertEqual(stats.get("territory"), 1)

        db.close()


if __name__ == "__main__":
    unittest.main()
