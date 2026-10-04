"""Diagnostic: does batched multi-parameter read work on the real boiler,
and which of the ~594 catalogued parameters actually respond on it?

Reuses the real PlumDevice driver from the integration (not a hand-rolled
protocol implementation like the older debug scripts) so this exercises
exactly the code path Home Assistant runs, including the fixed frame
validation, batching, and I/O locking.

Three phases:
  1. Cross-checks get_value() (single) against get_values() (batched) on a
     handful of known-good slugs -- proves batching decodes identically.
  2. Probes how the boiler responds when a batch mixes valid PIDs with one
     deliberately invalid PID -- determines whether a full-catalog scan
     can safely batch (fast) or must fall back to one-by-one (slow but
     immune to one bad PID poisoning a whole batch).
  3. Scans every slug in the device map with whichever strategy phase 2
     showed is safe, and writes a JSON report of responsive/unresponsive
     slugs with their current values.

Read-only: never calls set_value(). Safe to run against the live boiler.

Usage:
    python3 tools/scan_device_map.py [IP] [--batch-size N]
"""
import asyncio
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "custom_components" / "plum_ecomax"))
from plum_device import PlumDevice  # noqa: E402

MAP_FILE = REPO_ROOT / "custom_components" / "plum_ecomax" / "device_map_ecomax360i.json"
KNOWN_GOOD_SAMPLE = ["tempcwu", "hdwstate", "hdwpumpforce", "hdwtsetpoint", "tempbuforup"]
INVALID_PID = 65000  # far outside any real DP table index, per spec address space 1..65534


def _fmt(v, width=14):
    return f"{v!r:>{width}}"


async def phase1_cross_check(device: PlumDevice) -> bool:
    print("=" * 78)
    print("PHASE 1 — get_value() (une par une) vs get_values() (groupées)")
    print("=" * 78)
    sample = [s for s in KNOWN_GOOD_SAMPLE if s in device.params_map]
    if not sample:
        print("  Aucun des slugs connus n'est présent dans la device map, phase ignorée.")
        return True

    all_match = True
    for slug in sample:
        # A single read against real hardware can hit a transient hiccup
        # (dropped packet, boiler momentarily busy); retry the comparison
        # itself once before treating it as a genuine divergence.
        for attempt in (1, 2):
            single_val = await device.get_value(slug, retries=2)
            batch_val = (await device.get_values([slug], retries=2)).get(slug)
            if single_val == batch_val:
                break
        match = single_val == batch_val
        all_match &= match
        note = "OK" if match else f"MISMATCH persistant après 2 essais !!"
        print(f"  {slug:24s} single={_fmt(single_val)}  batch={_fmt(batch_val)}  {note}")

    print(f"\n  -> {'Batching fonctionne, valeurs identiques.' if all_match else 'DIVERGENCE DÉTECTÉE — à investiguer avant de faire confiance au batching.'}")
    return all_match


async def phase2_probe_invalid_pid_in_batch(device: PlumDevice) -> str:
    print()
    print("=" * 78)
    print("PHASE 2 — comportement d'un lot mêlant PID valides et PID invalide")
    print("=" * 78)
    good = [s for s in KNOWN_GOOD_SAMPLE if s in device.params_map][:2]
    if len(good) < 2:
        print("  Pas assez de slugs connus disponibles, phase ignorée -> prudence: scan un par un.")
        return "one_by_one"

    good_pids = [(device.params_map[s]["id"], device.params_map[s]) for s in good]
    fake_param = {"type": "SHORT_INT", "exponent": 0}
    mixed_items = [good_pids[0], (INVALID_PID, fake_param), good_pids[1]]

    result = await device._read_values_batch(mixed_items)

    good_pid_values = {pid: result.get(pid) for pid, _ in good_pids}
    print(f"  Lot envoyé : {good[0]} (pid={good_pids[0][0]}), pid invalide={INVALID_PID}, {good[1]} (pid={good_pids[1][0]})")
    print(f"  Résultat : {len(result)}/3 valeurs reçues -- {result}")

    if all(v is not None for v in good_pid_values.values()):
        print("  -> Les PID valides répondent MÊME si un PID du même lot est invalide.")
        print("     Le scan complet peut utiliser le batching en toute sécurité.")
        return "batched"

    print("  -> Un PID invalide dans le lot empêche de lire les PID valides du même lot")
    print("     (la chaudière rejette probablement le lot entier). Le scan complet")
    print("     repassera en 'un paramètre à la fois' pour ne pas perdre de faux négatifs.")
    return "one_by_one"


async def phase3_full_scan(device: PlumDevice, strategy: str, batch_size: int):
    print()
    print("=" * 78)
    print(f"PHASE 3 — scan complet de {len(device.params_map)} paramètres (stratégie: {strategy})")
    print("=" * 78)

    all_slugs = list(device.params_map.keys())
    t0 = time.time()

    if strategy == "batched":
        results = await device.get_values(all_slugs, retries=2, batch_size=batch_size)
    else:
        results = {}
        for i, slug in enumerate(all_slugs, 1):
            val = await device.get_value(slug, retries=2)
            if val is not None:
                results[slug] = val
            if i % 50 == 0:
                print(f"  ... {i}/{len(all_slugs)} testés, {len(results)} réponses jusqu'ici ({time.time()-t0:.0f}s)")

    elapsed = time.time() - t0
    responsive = sorted(results.keys())
    unresponsive = sorted(set(all_slugs) - set(results.keys()))

    print(f"\n  Terminé en {elapsed:.1f}s.")
    print(f"  {len(responsive)}/{len(all_slugs)} paramètres ont répondu.")
    print(f"  {len(unresponsive)} n'ont pas répondu (absents sur cette chaudière, ou type RAW non décodé).")

    captures_dir = Path(__file__).parent / "captures"
    captures_dir.mkdir(exist_ok=True)
    out_path = captures_dir / f"dp_scan_{int(time.time())}.json"
    out_path.write_text(json.dumps({
        "ip": device.ip,
        "timestamp": time.time(),
        "strategy": strategy,
        "elapsed_seconds": round(elapsed, 1),
        "total_in_map": len(all_slugs),
        "responsive": {slug: results[slug] for slug in responsive},
        "unresponsive": unresponsive,
    }, indent=2, default=str))
    print(f"\n  Résultats écrits dans {out_path.name}")
    return out_path


async def main():
    ip = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "192.168.1.38"
    batch_size = 16
    if "--batch-size" in sys.argv:
        batch_size = int(sys.argv[sys.argv.index("--batch-size") + 1])

    device = PlumDevice(ip, map_file=str(MAP_FILE))
    device.load_map()
    print(f"Chargé {len(device.params_map)} paramètres depuis {MAP_FILE.name}")
    print(f"Cible : {ip}:8899\n")

    match = await phase1_cross_check(device)
    if not match:
        print("\nArrêt : phase 1 a détecté une divergence, pas la peine de continuer.")
        return

    strategy = await phase2_probe_invalid_pid_in_batch(device)
    await phase3_full_scan(device, strategy, batch_size)


if __name__ == "__main__":
    asyncio.run(main())
