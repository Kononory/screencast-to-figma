from flask import jsonify


def error_response(message: str, status_code: int):
    return jsonify({"error": message}), status_code


def bad_request(message: str):
    return error_response(message, 400)


def not_found(message: str = "not found"):
    return error_response(message, 404)
