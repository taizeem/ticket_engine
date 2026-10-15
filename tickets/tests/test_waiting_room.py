import time
import concurrent.futures
from django.test import SimpleTestCase

from tickets.waiting_room import get_redis_client
from tickets.waiting_room.token_bucket import RedisTokenBucket
from tickets.waiting_room.queue_manager import VirtualWaitingRoom
from tickets.waiting_room.admission import generate_admission_pass, verify_admission_pass


class RedisWaitingRoomIntegrationTest(SimpleTestCase):
    """
    SimpleTestCase does not instantiate a PostgreSQL database,
    making it ideal for pure Redis cache & algorithm tests.
    """

    def setUp(self):
        super().setUp()
        self.redis = get_redis_client()
        self.event_id = 9999
        self.waiting_room = VirtualWaitingRoom(event_id=self.event_id)
        self.waiting_room.clear()
        self.redis.delete(f"tb:event:{self.event_id}")

    def tearDown(self):
        self.waiting_room.clear()
        self.redis.delete(f"tb:event:{self.event_id}")
        super().tearDown()

    def test_token_bucket_burst_capacity_and_exhaustion(self):
        # 10 tokens/sec refill rate, 5 tokens burst capacity
        bucket = RedisTokenBucket(refill_rate=10.0, capacity=5)

        # First 5 calls consume burst capacity
        for _ in range(5):
            allowed, _ = bucket.consume(event_id=self.event_id)
            self.assertTrue(allowed)

        # 6th immediate call must be rejected
        allowed, remaining = bucket.consume(event_id=self.event_id)
        self.assertFalse(allowed)
        self.assertLess(remaining, 1.0)

        # Sleep 250ms -> Refills ~2.5 tokens
        time.sleep(0.25)
        allowed, remaining = bucket.consume(event_id=self.event_id)
        self.assertTrue(allowed)

    def test_token_bucket_concurrent_threads(self):
        """
        Verify that under 20 concurrent threads with a bucket capacity of 5,
        exactly 5 threads succeed and 15 threads are rejected without data races.
        """
        bucket = RedisTokenBucket(refill_rate=0.1, capacity=5)
        success_count = 0
        failure_count = 0

        def try_consume():
            return bucket.consume(event_id=self.event_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(try_consume) for _ in range(20)]
            for future in concurrent.futures.as_completed(futures):
                allowed, _ = future.result()
                if allowed:
                    success_count += 1
                else:
                    failure_count += 1

        self.assertEqual(success_count, 5)
        self.assertEqual(failure_count, 15)

    def test_zset_fifo_queue_ordering(self):
        # Enqueue 3 users sequentially
        pos1, is_new1 = self.waiting_room.enqueue("user_alpha")
        time.sleep(0.01)
        pos2, is_new2 = self.waiting_room.enqueue("user_beta")
        time.sleep(0.01)
        pos3, is_new3 = self.waiting_room.enqueue("user_gamma")

        self.assertTrue(is_new1)
        self.assertTrue(is_new2)
        self.assertTrue(is_new3)
        self.assertEqual(pos1, 1)
        self.assertEqual(pos2, 2)
        self.assertEqual(pos3, 3)

        # Re-enqueueing user_alpha must NOT reset their position in line
        re_pos1, re_is_new1 = self.waiting_room.enqueue("user_alpha")
        self.assertFalse(re_is_new1)
        self.assertEqual(re_pos1, 1)

        # Pop top 2 users
        admitted = self.waiting_room.admit_next_batch(batch_size=2)
        self.assertEqual(len(admitted), 2)
        self.assertEqual(admitted[0][0], "user_alpha")
        self.assertEqual(admitted[1][0], "user_beta")

        # user_gamma should now be in position 1
        self.assertEqual(self.waiting_room.get_position("user_gamma"), 1)
        self.assertEqual(self.waiting_room.get_queue_size(), 1)

    def test_admission_pass_tampering_and_expiration(self):
        user_id = "user_secure_1"
        pass_token = generate_admission_pass(
            event_id=self.event_id,
            user_id=user_id,
            ttl_seconds=1,  # Short TTL for expiration test
        )

        # Valid pass verification
        self.assertTrue(
            verify_admission_pass(
                token=pass_token,
                expected_event_id=self.event_id,
                expected_user_id=user_id,
            )
        )

        # Mismatched user_id check
        self.assertFalse(
            verify_admission_pass(
                token=pass_token,
                expected_event_id=self.event_id,
                expected_user_id="impostor_user",
            )
        )

        # Tampered payload check
        tampered_token = pass_token[:-5] + "AAAAA"
        self.assertFalse(
            verify_admission_pass(
                token=tampered_token,
                expected_event_id=self.event_id,
                expected_user_id=user_id,
            )
        )

        # Expired pass check
        time.sleep(1.2)
        self.assertFalse(
            verify_admission_pass(
                token=pass_token,
                expected_event_id=self.event_id,
                expected_user_id=user_id,
            )
        )