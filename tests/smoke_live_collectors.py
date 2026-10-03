"""Vérification ponctuelle des collecteurs réels (Docker + Supabase), sans sondage tiers."""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "End"))
os.chdir(ROOT / "End")

import db  # noqa: E402
import telemetry as T  # noqa: E402


async def main() -> int:
    # Aucune sonde tierce n'est exécutée : on isole la collecte Docker + base.
    db.get_all_active_monitors = lambda: []          # type: ignore[assignment]

    engine = T.TelemetryEngine()
    await engine.start()
    await asyncio.sleep(6.0)                          # laisse passer un cycle complet
    health = engine.get_fleet_health()
    stats = engine.get_fleet_stats()
    business = engine.get_business_kpis()
    diagnostics = engine.get_diagnostics()
    await engine.stop()

    print("santé        :", health.get("status"), "| db:",
          health.get("infrastructure", {}).get("database", {}).get("status"),
          "| docker:", health.get("infrastructure", {}).get("docker_engine", {}).get("status"),
          "| durée:", health.get("probe_duration_ms"), "ms")
    print("flotte       :", json.dumps(health.get("fleet", {}), ensure_ascii=False)[:200])
    print("ressources   :", stats.get("active_tenants"), "conteneurs |",
          stats.get("total_memory_usage_mb"), "Mo | stale:", stats.get("stale"))
    print("KPIs         :", business.get("total_clients"), "clients |",
          business.get("total_sites"), "sites | MRR:", business.get("mrr"),
          "| stale:", business.get("stale"), "| anomalies:", business.get("anomalies"))
    print("sondes       :", health.get("external_services", {}))
    print("diagnostics  :", json.dumps(diagnostics, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
