"""Start the development server:  python run.py

use_reloader is off on purpose: the reloader would start the app twice and both
copies would try to resume unfinished jobs.
"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
