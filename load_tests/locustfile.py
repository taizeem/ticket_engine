import uuid
import hashlib
import random
from decimal import Decimal
from locust import HttpUser, task, between


def solve_pow(nonce: str, difficulty: int) -> int:
    prefix = "0" * int(difficulty)
    solution = 0
    while True:
        digest = hashlib.sha256(f"{nonce}:{solution}".encode("utf-8")).hexdigest()
        if digest.startswith(prefix):
            return solution
        solution += 1


class FlashSaleBuyer(HttpUser):
    # Aggressive flash-sale cadence: 50ms to 200ms between user steps
    wait_time = between(0.05, 0.2)
    event_id = 1

    @task
    def execute_purchase_funnel(self):
        user_id = f"load_user_{uuid.uuid4().hex[:10]}"

        # Step 1: Check Redis edge availability
        with self.client.get(
            f"/api/events/{self.event_id}/availability/",
            name="1_check_availability",
            catch_response=True,
        ) as avail_res:
            if avail_res.status_code != 200:
                avail_res.failure(f"Availability failed: {avail_res.status_code}")
                return
            if avail_res.json().get("status") == "SOLD_OUT":
                avail_res.success()
                return

        # Step 2: Fetch Anti-Bot PoW Challenge
        with self.client.get(
            f"/api/events/{self.event_id}/challenge/?user_id={user_id}",
            name="2_fetch_pow_challenge",
            catch_response=True,
        ) as chal_res:
            if chal_res.status_code != 200:
                chal_res.failure(f"Challenge failed: {chal_res.status_code}")
                return
            challenge = chal_res.json()

        # Solve PoW puzzle locally
        solution = solve_pow(challenge["nonce"], challenge["difficulty"])

        # Step 3: Enter Waiting Room / Token Bucket Gate
        enter_payload = {
            "user_id": user_id,
            "pow": {
                "nonce": challenge["nonce"],
                "difficulty": challenge["difficulty"],
                "timestamp": challenge["timestamp"],
                "signature": challenge["signature"],
                "solution": solution,
            },
        }
        with self.client.post(
            f"/api/events/{self.event_id}/enter/",
            json=enter_payload,
            name="3_enter_waiting_room",
            catch_response=True,
        ) as enter_res:
            if enter_res.status_code == 410:
                # Circuit breaker tripped (Sold Out): expected under flash-sale depletion
                enter_res.success()
                return
            if enter_res.status_code == 202:
                # Enqueued in Redis ZSET: expected when token bucket throttles burst
                enter_res.success()
                return
            if enter_res.status_code != 200:
                enter_res.failure(f"Unexpected enter status: {enter_res.status_code}")
                return

            admission_pass = enter_res.json()["admission_pass"]

        # Step 4: Allocate Contiguous Seats (1 to 3 adjacent seats)
        quantity = random.choice([1, 2, 3])
        alloc_payload = {
            "user_id": user_id,
            "quantity": quantity,
            "contiguous": True,
            "allow_fallback": True,
        }
        with self.client.post(
            f"/api/events/{self.event_id}/allocate/",
            json=alloc_payload,
            headers={"X-Admission-Token": admission_pass},
            name="4_allocate_seats",
            catch_response=True,
        ) as alloc_res:
            if alloc_res.status_code == 409:
                # Inventory exhausted mid-flight: valid concurrency outcome
                alloc_res.success()
                return
            if alloc_res.status_code != 201:
                alloc_res.failure(f"Allocation failed: {alloc_res.status_code}")
                return

            reservation_data = alloc_res.json()
            reservation_id = reservation_data["reservation_id"]
            total_amount = sum(Decimal(s["price"]) for s in reservation_data["seats"])

        # Step 5: Idempotent Payment Confirmation
        confirm_payload = {
            "amount": str(total_amount),
            "payment_reference": f"ch_bench_{uuid.uuid4().hex[:10]}",
        }
        with self.client.post(
            f"/api/reservations/{reservation_id}/confirm/",
            json=confirm_payload,
            headers={"Idempotency-Key": f"idemp_{reservation_id}"},
            name="5_confirm_payment",
            catch_response=True,
        ) as conf_res:
            if conf_res.status_code in (200, 201):
                conf_res.success()
            else:
                conf_res.failure(f"Confirmation failed: {conf_res.status_code}")