# Phase 05 — synthetic traffic

- Branch: `simplify/20261026-222418/05-traffic`
- Base: `fe46afbe466ded741262ca8748ef15818e6f1c29` (Phase 01; no dependency on later code)
- Owned files: `python-producer/traffic.py`, `python-producer/Dockerfile`, new `tests/test_traffic.py`. `requirements.txt` and the saved assets are unchanged.
- Callers checked: Compose runs the image's `python -u traffic.py` with `GRPC_SERVER_ADDRESS` and `TRAFFIC_*`; `experiments/stack.py` sets the `TRAFFIC_*` profile; CI imports `TransactionGenerator` inside the image; the protobuf modules are copied from `proto/fraud/v1`.

## Responsibility

Generate valid, reproducible synthetic `TransactionRequest`s from the saved SDV model and send them one at a time to ingress.

## Changes

| Kind | Change |
| --- | --- |
| Fix (D10) | Model and user files are resolved next to `traffic.py` (`transaction-generator-models/`), not the working directory. The image now keeps the repository layout. |
| Refactor | `TrafficSettings.from_env` holds seed, user limit, interval, fraud probability, timeout and address, all validated; `send_loop` is the transport loop with injectable `sleep`/`should_stop` and returns outcome counts; `main` only wires signals and the channel. The workload semantics are written in the module docstring. |
| Behavior | New optional `TRAFFIC_TIMEOUT_SECONDS` (default 10 s, the previous fixed value). `KeyError`/`TypeError` are no longer swallowed and retried forever; they propagate as bugs. gRPC errors, `success=false` and rejected samples still back off 1 s and continue. The final log line reports acknowledged/failed counts. |

**Workload unchanged:** the same customer pool, IP scenario probabilities, SDV conditional sampling, seeding and ID generation. No lightweight fixture distribution was introduced; SDV remains the only generator, so no separate fixture claims are needed.

## Checks run

| Check | Result |
| --- | --- |
| `tests.test_traffic` (8) against the baseline `traffic.py`, in the existing producer image with source mounted read-only, `--network none` | 4 generator characterization checks passed (same seed ⇒ same sequence, different seed differs; requests pass ingress validation; ≤N users with home-IP reuse >40/60; argument validation). **D10 failed**: `FileNotFoundError: 'model_gaussian_20L.pkl'` from another directory; 3 new-seam tests absent |
| Same 8 tests after the change | passed |
| `docker build` → `fraud-simplify-20261026/python-producer:phase05`; CI-style import and request inside the built image (no mount) | passed |
| Built image with `TRAFFIC_INTERVAL_SECONDS=-1` | fails at startup with the validation error |
| Built image against a closed port, then `docker stop` (SIGTERM) | 11 failed sends logged and backed off; clean exit 0 with `{'acknowledged': 0, 'failed': 11}`; container removed |

## Limitations

Reproducibility holds for one generator per process because SDV uses NumPy's global random state; timestamps are wall-clock. The actual send rate (interval plus round-trip) is not measured here. The SDV/torch image is heavy (CPU torch 2.5.1); making it optional was not attempted because SDV needs it.

## Why it is easier to follow

Settings, generation and transport are three separate pieces; the loop's failure policy is stated once and tested without a server.
