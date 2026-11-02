#!/usr/bin/env python3
"""Collect reproducible JosChat research evidence in an ISOLATED PostgreSQL test DB.

Runs the production SQL functions from supabase/schema.sql. Creates only synthetic
payloads. It does NOT measure live delivery latency, live WebRTC reliability or
historical dissertation trials. NEVER point TEST_DATABASE_URL at production.
"""
import argparse
import csv
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"
SIZES = (100, 500, 1000, 5000)


def require_test_database(url):
    name = urlparse(url).path.strip("/").split("/")[0].lower()
    if "test" not in name or not name:
        raise SystemExit("Refusing to operate on a database without 'test' in its name")
    if os.getenv("JOSCHAT_EVIDENCE_ALLOW_RESET") != "1":
        raise SystemExit("Set JOSCHAT_EVIDENCE_ALLOW_RESET=1 for an isolated disposable DB")


def query_one(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def initialize(conn):
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS private CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS auth CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute((ROOT / "tests/sql/supabase_stub.sql").read_text())
        cur.execute((ROOT / "supabase/schema.sql").read_text())
        cur.execute(
            "INSERT INTO auth.users(id,email) VALUES (%s,'a@example.invalid'),(%s,'b@example.invalid')",
            (USER_A, USER_B),
        )
        cur.execute("INSERT INTO profiles(id, username) VALUES (%s,'alice'),(%s,'bob')", (USER_A, USER_B))
        cur.execute("INSERT INTO conversations(created_by) VALUES (%s) RETURNING id", (USER_A,))
        conv = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO conversation_participants(conversation_id,user_id) VALUES (%s,%s),(%s,%s)",
            (conv, USER_A, conv, USER_B),
        )
    conn.autocommit = False
    return conv


def add_message(cur, conv, sender, ciphertext):
    digest = hashlib.sha256(ciphertext.encode("utf-8")).hexdigest()
    cur.execute("SELECT idx, block_hash FROM add_block(%s,%s::uuid,%s)", (digest, sender, conv))
    index, block_hash = cur.fetchone()
    cur.execute(
        "INSERT INTO messages(conversation_id,sender_id,encrypted_content,block_index,block_hash) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (conv, sender, ciphertext, index, block_hash),
    )
    return cur.fetchone()[0], index


def chain_valid(cur):
    return query_one(cur, "SELECT is_valid FROM validate_chain()")


def verify_all_messages(cur):
    cur.execute("SELECT id, validate_message(id) FROM messages ORDER BY id")
    return cur.fetchall()


def observe_probe(conn, label, mutate, expected_detected, notes):
    cur = conn.cursor()
    cur.execute("SAVEPOINT evidence_probe")
    try:
        observed = mutate(cur)
        return {
            "scenario": label,
            "expected_detection": expected_detected,
            "detected": observed,
            "as_expected": observed == expected_detected,
            "interpretation": notes,
        }
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT evidence_probe")
        cur.execute("RELEASE SAVEPOINT evidence_probe")
        cur.close()


def attack_probes(conn, ids, indices):
    first_id, last_id = ids[0], ids[-1]
    first_idx, last_idx = indices[0], indices[-1]
    mid_idx = indices[len(indices) // 2]
    probes = []

    def mutated_block_hash(cur):
        cur.execute("UPDATE blocks SET message_hash = 'CHANGED' WHERE index = %s", (mid_idx,))
        return not chain_valid(cur)

    probes.append(observe_probe(conn, "changed_block_message_hash", mutated_block_hash, True,
                                "The full chain detects alteration when stored hashes are not recomputed."))

    def mutated_predecessor(cur):
        cur.execute("UPDATE blocks SET previous_hash = %s WHERE index = %s", ("f" * 64, mid_idx))
        return not chain_valid(cur)

    probes.append(observe_probe(conn, "changed_previous_hash", mutated_predecessor, True,
                                "An inconsistent predecessor link is detected."))

    def deleted_message(cur):
        cur.execute("DELETE FROM messages WHERE id = %s", (first_id,))
        return not chain_valid(cur)

    probes.append(observe_probe(conn, "deleted_message_row", deleted_message, False,
                                "A surviving orphaned block does not prove that its message row was deleted."))

    def deleted_tail(cur):
        cur.execute("DELETE FROM messages WHERE id = %s", (last_id,))
        cur.execute("DELETE FROM blocks WHERE index = %s", (last_idx,))
        return not chain_valid(cur)

    probes.append(observe_probe(conn, "deleted_latest_message_and_block", deleted_tail, False,
                                "Without an external tip checkpoint, truncating the newest record is not detected."))

    def reassigned_message(cur):
        cur.execute(
            "UPDATE messages SET block_index = %s, block_hash = "
            "(SELECT block_hash FROM blocks WHERE index = %s) WHERE id = %s",
            (indices[1], indices[1], first_id),
        )
        return not query_one(cur, "SELECT validate_message(%s)", (first_id,))

    probes.append(observe_probe(conn, "reassigned_message_block", reassigned_message, True,
                                "Moving ciphertext to another message block breaks the content binding."))

    def rewritten_entire_chain(cur):
        new_ciphertext = "synthetic-privileged-rewrite"
        new_hash = hashlib.sha256(new_ciphertext.encode()).hexdigest()
        cur.execute("UPDATE messages SET encrypted_content = %s WHERE id = %s", (new_ciphertext, first_id))
        cur.execute("UPDATE blocks SET message_hash = %s WHERE index = %s", (new_hash, first_idx))
        cur.execute("SELECT index FROM blocks ORDER BY index")
        previous = "0" * 64
        for (index,) in cur.fetchall():
            cur.execute(
                "UPDATE blocks SET previous_hash = %s, "
                "block_hash = compute_block_hash(index,created_at,message_hash,sender_id,conversation_id,%s) "
                "WHERE index = %s RETURNING block_hash",
                (previous, previous, index),
            )
            previous = cur.fetchone()[0]
            cur.execute("UPDATE messages SET block_hash = %s WHERE block_index = %s", (previous, index))
        return (not chain_valid(cur)) or any(not verified for _, verified in verify_all_messages(cur))

    probes.append(observe_probe(conn, "privileged_complete_chain_rewrite", rewritten_entire_chain, False,
                                "A privileged writer can recompute all hashes. Independent anchoring is needed."))
    conn.commit()
    return probes


def integrity_experiment(conn, conv, seed):
    rng = random.Random(seed)
    cur = conn.cursor()
    ids, indices = [], []
    for i in range(498):
        payload = "synthetic-ciphertext-{0:04d}-{1:032x}".format(i, rng.getrandbits(128))
        msg_id, idx = add_message(cur, conv, USER_A if i % 2 == 0 else USER_B, payload)
        ids.append(msg_id)
        indices.append(idx)
    conn.commit()
    assert chain_valid(cur)
    initial = verify_all_messages(cur)
    assert len(initial) == 498 and all(verified for _, verified in initial), "Clean baseline invalid"
    probes = attack_probes(conn, ids, indices)

    changed = set(rng.sample(ids, 150))
    cur.execute(
        "UPDATE messages SET encrypted_content = encrypted_content || '-altered' WHERE id = ANY(%s)",
        (list(changed),),
    )
    conn.commit()
    outcomes = []
    tp = fp = fn = tn = 0
    for message_id, verified in verify_all_messages(cur):
        tampered = message_id in changed
        detected = not verified
        if tampered and detected:
            tp += 1
        elif tampered and not detected:
            fn += 1
        elif not tampered and detected:
            fp += 1
        else:
            tn += 1
        outcomes.append({"case_id": message_id, "deliberately_tampered": int(tampered),
                         "detected": int(detected)})
    counts = {"TP": tp, "TN": tn, "FP": fp, "FN": fn, "total": len(outcomes)}
    denominators = {
        "sensitivity": tp + fn,
        "specificity": tn + fp,
        "accuracy": tp + tn + fp + fn,
        "false_positive_rate": fp + tn,
    }
    metrics = {
        "sensitivity": tp / denominators["sensitivity"] if denominators["sensitivity"] else None,
        "specificity": tn / denominators["specificity"] if denominators["specificity"] else None,
        "accuracy": (tp + tn) / denominators["accuracy"] if denominators["accuracy"] else None,
        "false_positive_rate": fp / denominators["false_positive_rate"] if denominators["false_positive_rate"] else None,
    }
    assert len(outcomes) == 498 and len(changed) == 150
    # This expectation checks this narrowly defined ciphertext-mutation experiment,
    # not all conceivable attacks. Weaknesses are recorded separately above.
    assert (tp, tn, fp, fn) == (150, 348, 0, 0), counts
    cur.close()
    return {"design": "498 synthetic ciphertext rows; seeded selection of 150 altered rows",
            "sample_seed": seed, "confusion_matrix": counts, "metrics": metrics,
            "attack_probes": probes}, outcomes


def timing_experiment(conn, conv, repeat):
    cur = conn.cursor()
    records = []
    for size in SIZES:
        cur.execute("TRUNCATE TABLE messages, blocks RESTART IDENTITY CASCADE")
        for i in range(size):
            ciphertext = "synthetic-timing-{0:04d}".format(i)
            cur.execute("SELECT idx FROM add_block(%s,%s::uuid,%s)",
                        (hashlib.sha256(ciphertext.encode()).hexdigest(), USER_A, conv))
        conn.commit()
        assert chain_valid(cur)
        for trial in range(repeat):
            start = time.perf_counter_ns()
            cur.execute("SELECT is_valid, blocks_checked FROM validate_chain()")
            valid, count = cur.fetchone()
            ms = (time.perf_counter_ns() - start) / 1_000_000
            if not valid or count != size:
                raise AssertionError("Validation failure for chain size {0}".format(size))
            records.append({"chain_size": size, "trial": trial + 1, "validation_ms": round(ms, 6)})
    cur.close()
    summary = []
    for size in SIZES:
        samples = sorted(x["validation_ms"] for x in records if x["chain_size"] == size)
        p95_index = max(0, int((len(samples) * 0.95 + 0.999999)) - 1)
        summary.append({
            "blocks": size, "trials": repeat,
            "mean_ms": round(statistics.mean(samples), 4),
            "median_ms": round(statistics.median(samples), 4),
            "stdev_ms": round(statistics.stdev(samples), 4) if len(samples) >= 2 else 0,
            "p95_ms": round(samples[min(p95_index, len(samples) - 1)], 4),
            "min_ms": min(samples), "max_ms": max(samples),
        })
    return summary, records


def write_csv(path, records):
    if not records:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def markdown_report(report):
    m = report["integrity"]["confusion_matrix"]
    q = report["integrity"]["metrics"]
    out = [
        "# JosChat evidence — synthetic test database",
        "",
        "Generated (UTC): " + report["generated_at"],
        "",
        "Commit: " + report["commit"],
        "",
        "**Scope:** Existing production PostgreSQL hash/validation functions on a disposable "
        "database; deterministic synthetic inputs. This is NOT the historical dissertation dataset.",
        "",
        "## Integrity: 498 cases",
        "",
        "| TP | TN | FP | FN | Sensitivity | Specificity | Accuracy | False-positive rate |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        "| {TP} | {TN} | {FP} | {FN} | {s:.2%} | {sp:.2%} | {a:.2%} | {f:.2%} |".format(
            **m, s=q["sensitivity"], sp=q["specificity"], a=q["accuracy"], f=q["false_positive_rate"]),
        "",
        "**Important:** 100% on this synthetic, specific mutation set does not prove "
        "complete chain immutability, secure endpoints, or field-security effectiveness.",
        "",
        "## Additional attack probes",
        "",
        "| Scenario | Detected by current verifier? | Security interpretation |",
        "|---|---|---|",
    ]
    for probe in report["integrity"]["attack_probes"]:
        out.append("| {0} | {1} | {2} |".format(
            probe["scenario"], "Yes" if probe["detected"] else "No", probe["interpretation"]))
    out += [
        "",
        "## Full-chain validation timing",
        "",
        "| Chain size | Trials | Mean (ms) | Median (ms) | Std dev (ms) | p95 (ms) |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for t in report["timings"]:
        out.append("| {blocks} | {trials} | {mean_ms} | {median_ms} | {stdev_ms} | {p95_ms} |".format(**t))
    out += [
        "",
        "These times reflect the GitHub runner + local PostgreSQL service, not user-to-user delivery.",
        "",
        "## Not verified by this workflow",
        "",
        "- Historical **1.6 second** message delivery average (requires timestamped real-client trials).",
        "- Historical **0.38 second** 500-block verification time (runner/hardware conditions differ).",
        "- Historical **94.2%** WebRTC connection success (requires original call attempts and network metadata).",
        "- Insecurity reduction, completed field usability testing, and independent historical dataset provenance.",
        "- This local setup does not use the production Supabase infrastructure or live cellular networks.",
        "",
    ]
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="evidence-output")
    p.add_argument("--repeats", type=int, default=15)
    p.add_argument("--seed", type=int, default=20261103)
    args = p.parse_args()
    if not 3 <= args.repeats <= 100:
        p.error("--repeats must be between 3 and 100")
    url = os.environ.get("TEST_DATABASE_URL", "")
    require_test_database(url)
    destination = Path(args.output)
    destination.mkdir(parents=True, exist_ok=True)
    with psycopg2.connect(url) as conn:
        conv = initialize(conn)
        with conn.cursor() as cur:
            pg_version = query_one(cur, "SHOW server_version")
        integrity, outcomes = integrity_experiment(conn, conv, args.seed)
        timings, samples = timing_experiment(conn, conv, args.repeats)
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        sha = os.getenv("GITHUB_SHA", "unknown")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": sha,
        "runner": {"python": sys.version.split()[0], "platform": platform.platform(),
                   "postgres_version": pg_version, "psycopg2": psycopg2.__version__},
        "integrity": integrity, "timings": timings,
        "evidence_scope": "isolated_postgresql_synthetic_not_live_field_trials",
    }
    (destination / "evidence.json").write_text(json.dumps(report, indent=2) + "\n")
    (destination / "REPORT.md").write_text(markdown_report(report))
    write_csv(destination / "integrity_cases.csv", outcomes)
    write_csv(destination / "timing_trials.csv", samples)
    write_csv(destination / "attack_probes.csv", integrity["attack_probes"])
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(markdown_report(report) + "\n")
    print(markdown_report(report))


if __name__ == "__main__":
    main()
