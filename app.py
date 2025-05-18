from flask import Flask, request, render_template, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
from azure.storage.blob import BlobServiceClient
import pyodbc
import os
import uuid
import requests
from opencensus.ext.azure.log_exporter import AzureLogHandler
import logging
from dotenv import load_dotenv
import threading

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Azure Blob Setup
blob_service_client = BlobServiceClient.from_connection_string(os.getenv("AZURE_STORAGE_CONNECTION_STRING"))
container_name = "complaint-images"
try:
    blob_service_client.create_container(container_name)
except Exception:
    pass  # Container likely already exists

server = 'complaint-database-16.database.windows.net'
port = 1433
database = 'complaint-1612'
username = 'admin-database'
password = 'Jatin@1612'
driver = '{ODBC Driver 18 for SQL Server}'

# Azure SQL Setup
conn_str = os.getenv("AZURE_SQL_CONN_STRING")

# Logic App Webhook URL
logic_app_url = os.getenv("LOGIC_APP_WEBHOOK_URL")

# Monitoring - Azure Application Insights
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 1) Add a StreamHandler and explicitly give it a lock
stream_handler = logging.StreamHandler()
stream_handler.lock = threading.RLock()
logger.addHandler(stream_handler)

# 2) Then try AzureLogHandler
appinsights_conn = os.getenv("APPINSIGHTS_CONNECTION_STRING")
if appinsights_conn:
    try:
        ai_handler = AzureLogHandler(connection_string=appinsights_conn)
        ai_handler.lock = threading.RLock()
        logger.addHandler(ai_handler)
    except Exception as e:
        logger.warning("AzureLogHandler could not be added: %s", e)

@app.route("/")
def home():
    return redirect(url_for("submit_complaint"))

@app.route("/submit", methods=["GET", "POST"])
def submit_complaint():
    if request.method == "POST":
        try:
            title = request.form["title"]
            description = request.form["description"]
            type_ = request.form["type"]
            file = request.files.get("file")

            student_name = request.form.get("student_name")
            email = request.form.get("email")

            file_url = None

            # Upload file to Azure Blob
            if file and file.filename != "":
                # Basic file type check (allow only images)
                if not file.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                    return jsonify({"success": False, "error": "Invalid file type."}), 400
                # Basic size check (e.g., max 5MB)
                file.seek(0, 2)
                if file.tell() > 5 * 1024 * 1024:
                    return jsonify({"success": False, "error": "File too large."}), 400
                file.seek(0)
                filename = secure_filename(file.filename)
                blob_name = f"{uuid.uuid4()}_{filename}"
                blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
                blob_client.upload_blob(file)
                file_url = blob_client.url

            # Save to Azure SQL
            with pyodbc.connect(conn_str) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO Complaints (title, description, type, file_url, status, student_name, email)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (title, description, type_, file_url, "Submitted", student_name, email))
                conn.commit()

            # Send Email via Logic App
            payload = {
                "title": title,
                "description": description,
                "type": type_,
                "file_url": file_url,
                "status": "Submitted",
                "student_name": student_name,
                "email": email
            }
            requests.post(logic_app_url, json=payload)

            logger.info("Complaint submitted successfully")

            return redirect(url_for("student_dashboard"))

        except Exception as e:
            logger.error("Error while submitting complaint", exc_info=True)
            return jsonify({"success": False, "error": str(e)}), 500

    return render_template("submit_complaint.html")

@app.route("/dashboard")
def student_dashboard():
    return render_template("student_dashboard.html")

@app.route("/admin")
def admin_dashboard():
    return render_template("admin_dashboard.html")

@app.route("/get_complaints", methods=["GET"])
def get_complaints():
    try:
        with pyodbc.connect(conn_str) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, description, type, file_url, status, submitted_at
                FROM Complaints
                ORDER BY submitted_at DESC
            """)
            rows = cursor.fetchall()

            complaints = []
            for row in rows:
                complaints.append({
                    "id": row.id,
                    "title": row.title,
                    "description": row.description,
                    "type": row.type,
                    "file_url": row.file_url,
                    "status": row.status,
                    "submitted_at": row.submitted_at.strftime('%Y-%m-%d %H:%M:%S') if row.submitted_at else "N/A"
                })

            return jsonify({"complaints": complaints})
    except Exception as e:
        logger.error("Error fetching complaints", exc_info=True)
        return jsonify({"error": "Could not fetch complaints"}), 500

# 🔁 New: Assign complaint to admin/staff
@app.route("/assign_complaint", methods=["POST"])
def assign_complaint():
    try:
        data = request.get_json()
        complaint_id = data.get("id")
        assignee = data.get("assignee")

        with pyodbc.connect(conn_str) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE Complaints
                SET status = ?, assigned_to = ?
                WHERE id = ?
            """, ("Assigned", assignee, complaint_id))
            conn.commit()

        return jsonify({"success": True, "message": "Complaint assigned successfully."})
    except Exception as e:
        logger.error("Error assigning complaint", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500

# 🔁 New: Update complaint status
@app.route("/update_status", methods=["POST"])
def update_status():
    try:
        data = request.get_json()
        complaint_id = data.get("id")
        new_status = data.get("status")

        with pyodbc.connect(conn_str) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE Complaints
                SET status = ?
            """, (new_status, complaint_id))
            conn.commit()

        return jsonify({"success": True, "message": "Complaint status updated successfully."})
    except Exception as e:
        logger.error("Error updating complaint status", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)