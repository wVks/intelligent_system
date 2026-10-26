import json
from pathlib import Path

counts = json.loads(Path(__file__).with_name("observed-summary.json").read_text())
assert counts["total"] > 0
assert sum(counts[key] for key in ("success", "timeout", "no_result")) == counts["total"]
for key in ("success", "timeout", "no_result"):
    print(f"{key}: {counts[key]}/{counts['total']} = {100 * counts[key] / counts['total']:.1f}%")
print("Источник: агрегаты из задания. Provider: fake. Качество содержания не измерено.")
