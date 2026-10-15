# tickets/tests/test_pow_gate.py
import json
import time
import concurrent.futures
from decimal import Decimal
from datetime import timedelta
from django.test import TransactionTestCase, Client
from django.utils import timezone
from django.urls import reverse
from django.db import connections, connection

from tickets.models import Event, Seat, SeatStatus
from tickets.waiting_room import get_redis_client
from tickets.waiting_room.pow import (
    generate_pow_challenge,
    verify_pow_solution,
    solve_pow_challenge,
)


class AntiBotPoWGateTest(TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.redis = get_redis_client()
        self.now = timezone.now()

        self.event = Event.objects.create(
            name="Tomorrowland 2026 Mainstage",
            slug="tomorrowland-2026",
            sale_start=self.now - timedelta(hours=1),
            sale_end=self.now + timedelta(days=2),
            total_seats=50,
            is_active=True,
        )

        self.seats = [
            Seat(
                event=self.event,
                section="VIP",
                row="1",
                seat_number=i + 1,
                price=Decimal("350.00"),
                status=SeatStatus.AVAILABLE,
            )
            for i in range(10)
        ]
        Seat.objects.bulk_create(self.seats)

        self.redis.delete(f"tb:event:{self.event.id}")
        self.redis.delete(f"circuit_breaker:sold_out:{self.event.id}")

    def tearDown(self):
        self.redis.delete(f"tb:event:{self.event.id}")
        self.redis.delete(f"circuit_breaker:sold_out:{self.event.id}")
        connections.close_all()
        super().tearDown()

    def test_challenge_generation_and_verification(self):
        challenge_url = reverse("pow_challenge", kwargs={"event_id": self.event.id})
        response = self.client.get(challenge_url, {"user_id": "human_fan_1"})
        self.assertEqual(response.status_code, 200)
        challenge = response.json()
        self.assertIn("nonce", challenge)
        self.assertIn("signature", challenge)
        self.assertEqual(challenge["difficulty"], 4)

        test_challenge = generate_pow_challenge(
            event_id=self.event.id,
            user_id="human_fan_1",
            difficulty=3,
        )
        solution = solve_pow_challenge(test_challenge["nonce"], difficulty=3)

        is_valid, msg = verify_pow_solution(
            event_id=self.event.id,
            user_id="human_fan_1",
            nonce=test_challenge["nonce"],
            difficulty=3,
            timestamp=test_challenge["timestamp"],
            signature=test_challenge["signature"],
            solution=solution,
        )
        self.assertTrue(is_valid)
        self.assertEqual(msg, "Valid.")

    def test_forged_pow_signature_rejected(self):
        challenge = generate_pow_challenge(
            event_id=self.event.id,
            user_id="bot_01",
            difficulty=3,
        )
        solution = solve_pow_challenge(challenge["nonce"], difficulty=3)

        is_valid, msg = verify_pow_solution(
            event_id=self.event.id,
            user_id="bot_01",
            nonce=challenge["nonce"],
            difficulty=3,
            timestamp=challenge["timestamp"],
            signature="forged_signature_hex_value",
            solution=solution,
        )
        self.assertFalse(is_valid)
        self.assertEqual(msg, "Invalid challenge signature.")

    def test_anti_replay_spent_nonce_protection(self):
        challenge = generate_pow_challenge(
            event_id=self.event.id,
            user_id="human_fan_2",
            difficulty=3,
        )
        solution = solve_pow_challenge(challenge["nonce"], difficulty=3)

        is_valid1, _ = verify_pow_solution(
            event_id=self.event.id,
            user_id="human_fan_2",
            nonce=challenge["nonce"],
            difficulty=3,
            timestamp=challenge["timestamp"],
            signature=challenge["signature"],
            solution=solution,
        )
        self.assertTrue(is_valid1)

        is_valid2, msg = verify_pow_solution(
            event_id=self.event.id,
            user_id="human_fan_2",
            nonce=challenge["nonce"],
            difficulty=3,
            timestamp=challenge["timestamp"],
            signature=challenge["signature"],
            solution=solution,
        )
        self.assertFalse(is_valid2)
        self.assertEqual(msg, "Challenge solution has already been spent.")

    def test_difficulty_downgrade_tampering_rejected(self):
        # Attacker attempts to downgrade difficulty from 4 to 1 to reduce work
        challenge = generate_pow_challenge(
            event_id=self.event.id,
            user_id="downgrade_attacker",
            difficulty=4,
        )
        cheap_solution = solve_pow_challenge(challenge["nonce"], difficulty=1)

        is_valid, msg = verify_pow_solution(
            event_id=self.event.id,
            user_id="downgrade_attacker",
            nonce=challenge["nonce"],
            difficulty=1,  # Tampered difficulty
            timestamp=challenge["timestamp"],
            signature=challenge["signature"],
            solution=cheap_solution,
        )
        self.assertFalse(is_valid)
        self.assertEqual(msg, "Invalid challenge signature.")

    def test_user_id_harvesting_cross_submission_rejected(self):
        # Challenge generated for user_alice submitted by user_bob
        challenge = generate_pow_challenge(
            event_id=self.event.id,
            user_id="user_alice",
            difficulty=3,
        )
        solution = solve_pow_challenge(challenge["nonce"], difficulty=3)

        is_valid, msg = verify_pow_solution(
            event_id=self.event.id,
            user_id="user_bob",  # Mismatched user_id
            nonce=challenge["nonce"],
            difficulty=3,
            timestamp=challenge["timestamp"],
            signature=challenge["signature"],
            solution=solution,
        )
        self.assertFalse(is_valid)
        self.assertEqual(msg, "Invalid challenge signature.")

    def test_expired_challenge_rejected(self):
        # Backdate timestamp beyond the 60s TTL
        backdated_timestamp = int(time.time()) - 120
        challenge = generate_pow_challenge(
            event_id=self.event.id,
            user_id="slow_client",
            difficulty=2,
        )
        solution = solve_pow_challenge(challenge["nonce"], difficulty=2)

        is_valid, msg = verify_pow_solution(
            event_id=self.event.id,
            user_id="slow_client",
            nonce=challenge["nonce"],
            difficulty=2,
            timestamp=backdated_timestamp,
            signature=challenge["signature"],
            solution=solution,
            ttl_seconds=60,
        )
        self.assertFalse(is_valid)
        self.assertEqual(msg, "Challenge has expired.")

    def test_future_timestamp_manipulation_rejected(self):
        # Timestamp set far in the future
        future_timestamp = int(time.time()) + 300
        challenge = generate_pow_challenge(
            event_id=self.event.id,
            user_id="clock_manipulator",
            difficulty=2,
        )
        solution = solve_pow_challenge(challenge["nonce"], difficulty=2)

        is_valid, msg = verify_pow_solution(
            event_id=self.event.id,
            user_id="clock_manipulator",
            nonce=challenge["nonce"],
            difficulty=2,
            timestamp=future_timestamp,
            signature=challenge["signature"],
            solution=solution,
        )
        self.assertFalse(is_valid)
        self.assertEqual(msg, "Challenge has expired.")

    def test_incorrect_solution_fails(self):
        challenge = generate_pow_challenge(
            event_id=self.event.id,
            user_id="bad_math_client",
            difficulty=4,
        )
        # Passing an invalid random integer solution
        is_valid, msg = verify_pow_solution(
            event_id=self.event.id,
            user_id="bad_math_client",
            nonce=challenge["nonce"],
            difficulty=4,
            timestamp=challenge["timestamp"],
            signature=challenge["signature"],
            solution=999999999,
        )
        self.assertFalse(is_valid)
        self.assertIn("Incorrect proof-of-work solution", msg)

    def test_concurrent_replay_race_condition(self):
        # 10 concurrent threads attempt to spend the exact same solved challenge simultaneously
        challenge = generate_pow_challenge(
            event_id=self.event.id,
            user_id="race_attacker",
            difficulty=2,
        )
        solution = solve_pow_challenge(challenge["nonce"], difficulty=2)

        success_count = 0
        failure_count = 0

        def spend_challenge():
            try:
                valid, _ = verify_pow_solution(
                    event_id=self.event.id,
                    user_id="race_attacker",
                    nonce=challenge["nonce"],
                    difficulty=2,
                    timestamp=challenge["timestamp"],
                    signature=challenge["signature"],
                    solution=solution,
                )
                return valid
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(spend_challenge) for _ in range(10)]
            for future in concurrent.futures.as_completed(futures):
                if future.result():
                    success_count += 1
                else:
                    failure_count += 1

        # Exactly 1 thread must claim the nonce; 9 threads must be blocked by Redis SET NX
        self.assertEqual(success_count, 1)
        self.assertEqual(failure_count, 9)

    def test_enter_waiting_room_rejects_missing_pow(self):
        enter_url = reverse("enter_waiting_room", kwargs={"event_id": self.event.id})
        response = self.client.post(
            enter_url,
            data=json.dumps({"user_id": "naive_bot"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Missing Proof-of-Work", response.json()["error"])

    def test_enter_waiting_room_accepts_valid_pow_solution(self):
        challenge = generate_pow_challenge(
            event_id=self.event.id,
            user_id="good_client",
            difficulty=2,
        )
        solution = solve_pow_challenge(challenge["nonce"], difficulty=2)

        enter_url = reverse("enter_waiting_room", kwargs={"event_id": self.event.id})
        payload = {
            "user_id": "good_client",
            "pow": {
                "nonce": challenge["nonce"],
                "difficulty": challenge["difficulty"],
                "timestamp": challenge["timestamp"],
                "signature": challenge["signature"],
                "solution": solution,
            },
        }

        response = self.client.post(
            enter_url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertIn(response.status_code, [200, 202])
        self.assertIn(response.json()["status"], ["ADMITTED", "QUEUED"])