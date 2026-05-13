import json
import os
import uuid

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from processor.comparator import compare_sessions
from processor.pipeline import run_pipeline_from_file, run_pipeline_from_url
from server.job_queue import QueuedJob, local_job_queue
from server.job_store import job_store
from server.responses import bad_request, error_response, not_found


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


def register_routes(app: Flask) -> None:

    @app.route("/")
    def index():
        return send_from_directory("static", "index.html")

    @app.route("/process", methods=["POST"])
    def process():
        data = request.json
        video_url = data.get("video_url", "").strip()
        if not video_url:
            return bad_request("video_url required")

        job_id = str(uuid.uuid4())[:8]
        job_store.create(job_id)

        local_job_queue.enqueue(QueuedJob(
            job_id=job_id,
            kind="url",
            runner=lambda: run_pipeline_from_url(job_id, video_url),
        ))

        return jsonify({"job_id": job_id})

    @app.route("/upload", methods=["POST"])
    def upload():
        if "file" not in request.files:
            return bad_request("no file")

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

        job_store.create(job_id)
        local_job_queue.enqueue(QueuedJob(
            job_id=job_id,
            kind="file",
            runner=lambda: run_pipeline_from_file(
                job_id,
                video_path,
                consec,
                glob_t,
                classify,
                api_key,
                provider,
            ),
        ))

        return jsonify({"job_id": job_id})

    @app.route("/status/<job_id>")
    def status(job_id):
        job = job_store.get(job_id)
        if job is None:
            return not_found()
        return jsonify(job.to_status_response())

    @app.route("/manifest/<job_id>")
    def manifest(job_id):
        manifest_path = os.path.join("output", job_id, "manifest.json")
        if not os.path.exists(manifest_path):
            job = job_store.get(job_id)
            if job is None or job.status != "done":
                return error_response("not ready", 404)
        with open(manifest_path) as f:
            return jsonify(json.load(f))

    @app.route("/compare", methods=["POST"])
    def compare():
        data = request.get_json()
        job_a, job_b = data.get("job_a"), data.get("job_b")
        if not job_a or not job_b:
            return bad_request("provide job_a and job_b")

        path_a, path_b = f"sessions/{job_a}.json", f"sessions/{job_b}.json"
        if not os.path.exists(path_a) or not os.path.exists(path_b):
            return error_response("session profile not found — run analysis first", 404)

        with open(path_a) as f:
            profile_a = json.load(f)
        with open(path_b) as f:
            profile_b = json.load(f)

        api_key = data.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
        result = compare_sessions(profile_a, profile_b, api_key)
        if not result:
            return error_response("comparison failed", 500)

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
        job = job_store.get(job_id)
        if job is None:
            return "not found", 404
        return "<pre style='font:12px monospace;padding:16px'>" + "\n".join(job.log) + "</pre>"

    @app.route("/plugin-manifest/<job_id>")
    def plugin_manifest(job_id):
        manifest_path = os.path.join("output", job_id, "manifest.json")
        if not os.path.exists(manifest_path):
            job = job_store.get(job_id)
            if job is None or job.status != "done":
                return error_response("not ready", 404)

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
