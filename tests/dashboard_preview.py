"""Opt-in local UI fixture. Uses invented notes and no connected services."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "modal_agent"))
import dashboard

os.environ["DASHBOARD_PASSWORD"] = "local-preview"
notes = [{"name": "recipe.md", "title": "Pastéis de nata", "category": "culinary", "creator": "Cozinha portuguesa",
    "saved": 1791450000, "section": "notes", "status": "saved", "review_status": "approved", "version_id": "preview-v1",
    "url": "https://www.instagram.com/p/example/", "creator_url": "https://www.instagram.com/example/",
    "accounting": [{"record_id": "fixture", "step": "writer", "model": "fixture", "cost": .000041,
        "cost_status": "reported", "duration": 1.1, "success": True}],
    "content": "# Pastéis de nata\n\n**Category:** Culinary\n\nA crisp pastry shell with a soft custard filling.\n\n### Ingredients\n- 250 ml milk\n- 3 egg yolks\n- 100 g sugar\n- Puff pastry\n\n### Method\nWarm the milk. Mix the yolks and sugar, then add the milk gradually. Fill the pastry shells and bake until the tops are golden.\n",
    "generation": {"transcript": "[0–12s] Leite, gemas, açúcar e massa folhada.", "writer_history": [], "critique_history": ["approved"], "model_log": []}},
    {"name": "old.md", "title": "Pastéis de nata — earlier version", "category": "culinary", "creator": "Cozinha portuguesa",
     "saved": 1791400000, "section": "failed", "status": "superseded", "version_id": "preview-v0", "accounting": None,
     "content": "# Earlier recipe\n\nThe previous version remains available here."}]


def fixture_storage(operation, payload=None):
    if operation == "revision":
        return 1
    if operation == "list":
        return {"revision": 1, "records": [{k: v for k, v in n.items() if k != "content"} for n in notes]}
    if operation == "read":
        return next(n for n in notes if n["name"] == payload["name"])
    if operation == "search":
        return [n for n in notes if payload["query"].lower() in n["content"].lower()]
    return []


dashboard.storage = fixture_storage
dashboard.main()
