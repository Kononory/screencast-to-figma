import os
import shutil
import json


def save_to_disk(frame_paths: list[str], classifications: list[dict], output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    manifest = {}

    for path, item in zip(frame_paths, classifications):
        label = item["label"]
        if label in ("transition", "system_tray", "app_switcher", "home_screen"):
            continue

        parts = label.split("/", 1)
        parent = parts[0]
        child = parts[1] if len(parts) > 1 else None

        dest_dir = os.path.join(output_dir, label.replace("/", "__"))
        os.makedirs(dest_dir, exist_ok=True)

        idx = len(os.listdir(dest_dir))
        dest_path = os.path.join(dest_dir, f"{idx:03d}.jpg")
        shutil.copy2(path, dest_path)

        entry = {
            "path": dest_path,
            "key_text": item.get("key_text", ""),
            "components": item.get("components", []),
            "state": item.get("state", ""),
        }

        if parent not in manifest:
            manifest[parent] = {"children": {}}
        if child:
            manifest[parent]["children"].setdefault(child, [])
            manifest[parent]["children"][child].append(entry)
        else:
            manifest[parent].setdefault("images", [])
            manifest[parent]["images"].append(entry)

    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest_path
