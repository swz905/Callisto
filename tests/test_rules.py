from kairos.ingest.rules import extract_notification, file_parts, ride_status


def test_uber_eta_driver_destination():
    ex = extract_notification("com.ubercab", {"app": "Uber", "title": "Uber", "body": "Ramesh is 4 min away",
                                              "destination": "Office"})
    assert ex.kind == "ride" and ex.app == "Uber"
    assert ex.trip["eta_min"] == 4
    assert ex.trip["driver"] == "Ramesh"
    assert ex.trip["status"] == "arriving"
    assert ex.trip["destination"] == "Office"


def test_ride_statuses():
    assert ride_status("Ramesh has arrived at your pickup") == "arrived"
    assert ride_status("Trip completed. Rate your driver") == "completed"
    assert ride_status("Looking for a driver") == "requested"


def test_arrived_has_no_eta():
    ex = extract_notification("com.ubercab", {"title": "Uber", "body": "Your driver has arrived, 0 min"})
    assert ex.trip["status"] == "arrived" and ex.trip["eta_min"] is None


def test_email_sender_subject_link():
    ex = extract_notification("com.google.android.gm", {
        "title": "Rahul Sharma", "subject": "Q3 report for review",
        "body": "Please review: https://files.example.com/reports/Q3_report.pdf"})
    assert ex.kind == "message"
    assert ex.message["sender"] == "Rahul Sharma"
    assert ex.message["channel"] == "email"
    assert ex.message["subject"] == "Q3 report for review"
    assert ex.urls == ["https://files.example.com/reports/Q3_report.pdf"]


def test_whatsapp_group_sender():
    ex = extract_notification("com.whatsapp", {"title": "Family (3 messages)", "body": "Priya: dinner at 8?"})
    assert ex.message["sender"] == "Priya"
    assert ex.message["group"] == "Family"


def test_unknown_app():
    ex = extract_notification("com.random.app", {"title": "Hi", "body": "see https://x.io/a"})
    assert ex.kind == "other" and ex.urls == ["https://x.io/a"]


def test_file_parts_windows_and_posix():
    assert file_parts(r"C:\Users\arjun\Downloads\Q3_report.PDF") == ("Q3_report.PDF", "pdf", r"C:\Users\arjun\Downloads")
    assert file_parts("/sdcard/Download/a.zip") == ("a.zip", "zip", "/sdcard/Download")
