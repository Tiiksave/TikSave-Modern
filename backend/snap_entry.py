from app import app
from flask import send_from_directory
import os

FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))

@app.route("/__snapdeploy_test")
def snapdeploy_test():
    return {"status":"ok","service":"TikSave","frontend":os.path.exists(os.path.join(FRONTEND_DIR,"index.html"))}

# Replace the existing "/" handler with the frontend page.
for rule in list(app.url_map.iter_rules()):
    if rule.rule == "/" and rule.endpoint in app.view_functions:
        app.view_functions[rule.endpoint] = lambda: send_from_directory(FRONTEND_DIR, "index.html")
        break

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
