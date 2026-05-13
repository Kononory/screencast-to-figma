from flask import Flask

from server.cors import register_cors
from server.job_queue import local_job_queue
from server.routes import register_routes


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static")
    register_cors(app)
    register_routes(app)
    local_job_queue.start()
    return app
