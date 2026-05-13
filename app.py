from dotenv import load_dotenv

from server.app_factory import create_app

load_dotenv()
app = create_app()


if __name__ == "__main__":
    app.run(port=5055, debug=False)
