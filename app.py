import os
import shutil
import uuid
import json
import threading
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

from processor.downloader import download_video
from processor.extractor import extract_frames, filter_blank_frames
from processor.classifier import classify_frames
from processor.analyzer import analyze_ux
from processor.comparator import compare_sessions
from processor.results_store import save_to_disk

load_dotenv()

app = Flask(__name__, static_folder="static")

jobs: dict[str, dict] = {}


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/process", methods=["POST"])
def process():
    data = request.json
    video_url = data.get("video_url", "").strip()
    if not video_url:
        return jsonify({"error": "video_url required"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "queued", "log": [], "manifest_path": None, "progress": 0, "step": ""}

    thread = threading.Thread(target=_run_pipeline, args=(job_id, video_url), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400

    file = request.files["file"]
    job_id = str(uuid.uuid4())[:8]
    tmp_dir = os.path.join("tmp", job_id)
    os.makedirs(tmp_dir, exist_ok=True)

    filename = secure_filename(file.filename) or "video.mp4"
    video_path = os.path.join(tmp_dir, filename)
    file.save(video_path)

    consec = int(request.form.get("consec_threshold", 3))
    glob_t = int(request.form.get("global_threshold", 3))
    classify = request.form.get("classify", "true").lower() == "true"
    api_key = request.form.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
    provider = request.form.get("provider", "gemini")

    jobs[job_id] = {"status": "queued", "log": [], "manifest_path": None, "progress": 0, "step": ""}
    thread = threading.Thread(target=_run_pipeline_from_file, args=(job_id, video_path, consec, glob_t, classify, api_key, provider), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": job["status"], "log": job["log"], "progress": job["progress"], "step": job["step"],
                    "extracted": job.get("extracted", 0), "dupes": job.get("dupes", 0)})


@app.route("/manifest/<job_id>")
def manifest(job_id):
    manifest_path = os.path.join("output", job_id, "manifest.json")
    if not os.path.exists(manifest_path):
        job = jobs.get(job_id)
        if not job or job["status"] != "done":
            return jsonify({"error": "not ready"}), 404
    with open(manifest_path) as f:
        return jsonify(json.load(f))


@app.route("/compare", methods=["POST"])
def compare():
    data = request.get_json()
    job_a, job_b = data.get("job_a"), data.get("job_b")
    if not job_a or not job_b:
        return jsonify({"error": "provide job_a and job_b"}), 400

    path_a, path_b = f"sessions/{job_a}.json", f"sessions/{job_b}.json"
    if not os.path.exists(path_a) or not os.path.exists(path_b):
        return jsonify({"error": "session profile not found — run analysis first"}), 404

    with open(path_a) as f:
        profile_a = json.load(f)
    with open(path_b) as f:
        profile_b = json.load(f)

    api_key = data.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
    result = compare_sessions(profile_a, profile_b, api_key)
    if not result:
        return jsonify({"error": "comparison failed"}), 500

    return jsonify({"profile_a": profile_a, "profile_b": profile_b, "comparison": result})


@app.route("/sessions")
def list_sessions():
    if not os.path.exists("sessions"):
        return jsonify([])
    profiles = []
    for fname in os.listdir("sessions"):
        if fname.endswith(".json"):
            with open(f"sessions/{fname}") as f:
                p = json.load(f)
            profiles.append({
                "job_id": p["job_id"],
                "competitive_tier": p.get("competitive_tier", "")[:80],
                "funnel_length": len(p.get("funnel_sequence", [])),
                "paywall_position": p.get("paywall_position"),
                "has_downsell": p.get("has_downsell"),
            })
    return jsonify(profiles)


@app.route("/images/<job_id>/<path:filename>")
def serve_image(job_id, filename):
    return send_from_directory(os.path.join("output", job_id), filename)


@app.route("/log/<job_id>")
def job_log(job_id):
    job = jobs.get(job_id)
    if not job:
        return "not found", 404
    lines = job.get("log", [])
    return "<pre style='font:12px monospace;padding:16px'>" + "\n".join(lines) + "</pre>"


@app.route("/plugin-manifest/<job_id>")
def plugin_manifest(job_id):
    manifest_path = os.path.join("output", job_id, "manifest.json")
    if not os.path.exists(manifest_path):
        job = jobs.get(job_id)
        if not job or job["status"] != "done":
            return jsonify({"error": "not ready"}), 404

    with open(manifest_path) as f:
        raw = json.load(f)

    base = request.host_url.rstrip("/")
    output_root = os.path.abspath(os.path.join("output", job_id))
    sections = []

    for parent_key, parent_val in raw.items():
        if parent_key.startswith("_"):
            continue
        all_entries = list(parent_val.get("images", []))
        for entries in parent_val.get("children", {}).values():
            all_entries.extend(entries)
        if all_entries:
            sections.append({
                "name": parent_key.replace("_", " ").title(),
                "images": [_image_entry(e, parent_key, base, output_root, job_id) for e in all_entries]
            })

    return jsonify({"sections": sections, "analysis": raw.get("_analysis")})


def _image_entry(entry: dict, label: str, base: str, output_root: str, job_id: str) -> dict:
    abs_path = entry["path"] if isinstance(entry, dict) else entry
    rel = os.path.relpath(abs_path, output_root)
    return {
        "url": f"{base}/images/{job_id}/{rel}",
        "label": label,
        "key_text": entry.get("key_text", "") if isinstance(entry, dict) else "",
        "components": entry.get("components", []) if isinstance(entry, dict) else [],
        "state": entry.get("state", "") if isinstance(entry, dict) else "",
    }


def _log(job_id: str, msg: str):
    print(msg)
    jobs[job_id]["log"].append(msg)


def _step(job_id: str, label: str, progress: int):
    jobs[job_id]["step"] = label
    jobs[job_id]["progress"] = progress


def _run_pipeline_from_file(job_id: str, video_path: str, consec_threshold: int = 3, global_threshold: int = 3, classify: bool = True, api_key: str = "", provider: str = "gemini"):
    tmp_dir = os.path.dirname(video_path)
    output_dir = os.path.join("output", job_id)
    jobs[job_id]["status"] = "running"
    try:
        _step(job_id, "Loading file", 10)
        _log(job_id, f"Using local file: {os.path.basename(video_path)}")
        _process(job_id, video_path, tmp_dir, output_dir, consec_threshold, global_threshold, classify, api_key, provider)
    except Exception as e:
        jobs[job_id]["status"] = "error"
        _log(job_id, f"Error: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_pipeline(job_id: str, video_url: str, consec_threshold: int = 3, global_threshold: int = 3):
    tmp_dir = os.path.join("tmp", job_id)
    output_dir = os.path.join("output", job_id)
    os.makedirs(tmp_dir, exist_ok=True)
    jobs[job_id]["status"] = "running"
    try:
        _step(job_id, "Downloading", 10)
        _log(job_id, "Downloading video...")
        video_path = download_video(video_url, tmp_dir)
        _log(job_id, f"Video downloaded: {os.path.basename(video_path)}")
        _process(job_id, video_path, tmp_dir, output_dir, consec_threshold, global_threshold)
    except Exception as e:
        jobs[job_id]["status"] = "error"
        _log(job_id, f"Error: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _process(job_id: str, video_path: str, tmp_dir: str, output_dir: str, consec_threshold: int = 3, global_threshold: int = 3, classify: bool = True, api_key: str = "", provider: str = "gemini"):
    _step(job_id, "Extracting frames", 25)
    _log(job_id, f"Extracting frames (consec={consec_threshold}, global={global_threshold})...")
    frames = extract_frames(video_path, tmp_dir, consec_threshold=consec_threshold, global_threshold=global_threshold)
    frames, blank_count = filter_blank_frames(frames)
    if blank_count:
        _log(job_id, f"Removed {blank_count} blank frames (black/white)")
    _log(job_id, f"Extracted {len(frames)} frames")

    if classify:
        _step(job_id, "Classifying flows", 55)
        _log(job_id, "Classifying screens with AI...")
        classifications = classify_frames(frames, api_key, log_fn=lambda m: _log(job_id, m))

        _log(job_id, f"--- AI output ({len(frames)} frames) ---")
        for i, (path, item) in enumerate(zip(frames, classifications)):
            state_tag = f" [{item.get('state')}]" if item.get("state") else ""
            _log(job_id, f"  {i+1:02d} {item['label']} ({item['conf']:.2f}) \"{item.get('key_text','')}\"{state_tag}")

        # Deduplicate semantically: same label + key_text + components + state = same screen
        seen = set()
        unique_frames, unique_cls = [], []
        for path, item in zip(frames, classifications):
            text = item["key_text"].strip().lower()
            components_key = tuple(sorted(item.get("components", [])))
            state = item.get("state", "")
            if text:
                key = (item["label"], text, components_key, state)
            elif components_key:
                key = (item["label"], components_key, state)
            else:
                key = path
            if key not in seen:
                seen.add(key)
                unique_frames.append(path)
                unique_cls.append(item)
            else:
                _log(job_id, f"  DEDUP: {item['label']} \"{item.get('key_text','')}\" dropped")

        _log(job_id, f"--- {len(unique_frames)} unique screens (from {len(frames)}) ---")
        cats = sorted(set(c["label"] for c in unique_cls if c["label"] not in ("transition", "unsorted")))
        _log(job_id, f"Sections: {', '.join(cats) or 'none'}")
    else:
        _log(job_id, "AI classification skipped — importing all frames as-is")
        unique_frames = frames
        unique_cls = [{"label": "unsorted", "conf": 1.0, "key_text": "", "components": [], "state": ""} for _ in frames]
        _log(job_id, f"--- {len(unique_frames)} frames ---")

    analysis = None
    if classify and unique_frames:
        _step(job_id, "Analyzing", 75)
        _log(job_id, "Running UX analysis...")
        excluded = {"transition", "system_tray", "app_switcher", "home_screen", "unsorted"}
        candidates = [p for p, c in zip(unique_frames, unique_cls)
                      if c["label"].split("/")[0] not in excluded]
        if candidates:
            if len(candidates) > 20:
                step = len(candidates) / 20
                candidates = [candidates[int(i * step)] for i in range(20)]
            analysis = analyze_ux(candidates, api_key)
            _log(job_id, "UX analysis complete" if analysis else "UX analysis failed")

    _step(job_id, "Saving", 88)
    _log(job_id, "Saving results...")
    manifest_path = save_to_disk(unique_frames, unique_cls, output_dir)

    if analysis:
        with open(manifest_path) as f:
            mdata = json.load(f)
        mdata["_analysis"] = analysis
        with open(manifest_path, "w") as f:
            json.dump(mdata, f, indent=2)

        funnel = [c["label"] for c in unique_cls if c["label"] not in ("transition", "system_tray", "app_switcher")]
        paywall_pos = next((i for i, l in enumerate(funnel) if l.startswith("paywall")), None)
        profile = {
            "job_id": job_id,
            "funnel_sequence": funnel,
            "paywall_position": paywall_pos,
            "onboarding_count": sum(1 for l in funnel if l.startswith("onboarding")),
            "has_downsell": any(l.startswith("special_offer") for l in funnel),
            "strategy_coherence": analysis.get("strategy_coherence"),
            "competitive_tier": analysis.get("competitive_tier"),
            "monetization_hypothesis": analysis.get("monetization_hypothesis"),
            "onboarding_hypothesis": analysis.get("onboarding_hypothesis"),
            "feature_strategy_reasoning": analysis.get("feature_strategy_reasoning"),
        }
        os.makedirs("sessions", exist_ok=True)
        with open(f"sessions/{job_id}.json", "w") as f:
            json.dump(profile, f, indent=2)

    jobs[job_id]["manifest_path"] = manifest_path
    jobs[job_id]["extracted"] = len(frames)
    jobs[job_id]["dupes"] = len(frames) - len(unique_frames)
    jobs[job_id]["status"] = "done"
    jobs[job_id]["progress"] = 100
    jobs[job_id]["step"] = "Done"
    _log(job_id, f"Done. Job ID: {job_id}")


if __name__ == "__main__":
    app.run(port=5055, debug=False)
