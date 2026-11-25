# /// script
# dependencies = [
#   "streamlit",
#   "requests",
# ]
# ///

import streamlit as st
import requests
import json
import time
import os

st.set_page_config(page_title="FinSync App", layout="wide")

API_BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")

def get_headers():
    if "token" in st.session_state and st.session_state.token:
        return {"Authorization": f"Bearer {st.session_state.token}"}
    return {}

def api_get(path, params=None):
    return requests.get(f"{API_BASE}{path}", headers=get_headers(), params=params)

def api_post(path, json_data=None, data=None, files=None):
    return requests.post(f"{API_BASE}{path}", headers=get_headers(), json=json_data, data=data, files=files)

def api_delete(path, json_data=None):
    return requests.delete(f"{API_BASE}{path}", headers=get_headers(), json=json_data)

# State init
if "token" not in st.session_state:
    st.session_state.token = None
    st.session_state.user_email = None

def login_page():
    st.title("Sign in / Signup")
    
    t1, t2 = st.tabs(["Login", "Sign Up"])
    with t1:
        with st.form("login"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Login"):
                res = api_post("/auth/login", json_data={"email": email, "password": password})
                if res.status_code == 200:
                    st.session_state.token = res.json().get("access_token")
                    st.session_state.user_email = email
                    st.rerun()
                else:
                    st.error(f"Login failed: {res.text}")
                    
    with t2:
        with st.form("signup"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            first_name = st.text_input("First Name")
            if st.form_submit_button("Register"):
                res = api_post("/auth/register", json_data={
                    "email": email, 
                    "password": password, 
                    "first_name": first_name,
                    "phone_no": "1234567890"
                })
                if res.status_code == 201:
                    st.success("Registered successfully! Please log in.")
                else:
                    st.error(f"Registration failed: {res.text}")

def wait_for_task(status_path, label):
    with st.spinner(f"Waiting for {label}..."):
        for _ in range(30):
            res = api_get(status_path)
            if res.ok:
                state = res.json().get("state")
                if state == "SUCCESS":
                    st.success(f"{label} completed!")
                    return True
                elif state in ["FAILURE", "REVOKED"]:
                    st.error(f"{label} failed: {state}")
                    return False
            time.sleep(2)
        st.error(f"{label} timed out.")
        return False

def queue_report(path, payload, label):
    res = api_post(path, json_data=payload)
    if res.status_code == 202:
        task_id = res.json().get("task_id")
        wait_for_task(f"/api/reports/tasks/{task_id}", label)
    else:
        st.error(f"Failed to queue report: {res.text}")

def bank_rec_page():
    st.title("Bank Reconciliation")
    
    with st.form("rec_upload"):
        ledger_file = st.file_uploader("Ledger File")
        bank_file = st.file_uploader("Bank Statement")
        if st.form_submit_button("Upload & Run"):
            if ledger_file and bank_file:
                files = {
                    "ledger_file": (ledger_file.name, ledger_file.getvalue()),
                    "bank_file": (bank_file.name, bank_file.getvalue())
                }
                res = api_post("/api/bank-rec/reconciliation", files=files)
                if res.status_code == 202:
                    run_id = res.json().get("run_id")
                    st.session_state.last_run_id = run_id
                    st.success(f"Upload successful. Run ID: {run_id}")
                    wait_for_task(f"/api/bank-rec/reconciliation/{run_id}/status", "Reconciliation")
                else:
                    st.error(f"Upload failed: {res.text}")
                    
    if "last_run_id" in st.session_state:
        run_id = st.session_state.last_run_id
        st.subheader("Details about rec")
        res = api_get(f"/api/bank-rec/reconciliation/{run_id}")
        if res.ok:
            st.json(res.json())
            
        if st.button("Approve all"):
            res = api_post(f"/api/bank-rec/reconciliation/{run_id}/journal-entries/approve", json_data={"entries": []})
            if res.ok:
                st.success("Approved!")
            else:
                st.error(res.text)
                
        st.subheader("Files to be downloaded")
        if st.button("Request Report"):
            queue_report("/api/reports/bank-reconciliation", {"run_id": run_id}, "Bank Rec Report")

def ledger_page():
    st.title("Ledger")
    
    st.subheader("Upload bills")
    with st.form("upload_bills"):
        bill = st.file_uploader("Bill File")
        if st.form_submit_button("Upload"):
            if bill:
                res = api_post("/api/pipeline/bills", data={"direction": "input"}, files={"file": (bill.name, bill.getvalue())})
                if res.ok or res.status_code == 202:
                    st.success("Uploaded successfully!")
                else:
                    st.error(res.text)
                    
    st.subheader("Trial Balance")
    as_on = st.date_input("As On Date")
    if st.button("Fetch Trial Balance"):
        res = api_get("/api/ledger/trial-balance", params={"as_on": str(as_on)})
        if res.ok:
            st.json(res.json())
        else:
            st.error(res.text)
            
    st.subheader("Files to be downloaded")
    if st.button("Request Ledger Report"):
        queue_report("/api/reports/ledger", {"as_on": str(as_on)}, "Ledger Report")
        
def previous_runs_page():
    st.title("Previous Runs")
    st.write("Previous run ids along with bank rec and ledger for what run")
    # There is no explicit list-all runs in test.sh, but we can put a placeholder
    st.info("List of runs would appear here.")

def tds_page():
    st.title("TDS")
    st.write("Details about TDS")
    if st.button("Request TDS Report"):
        queue_report("/api/reports/tds", {"period_start": "2024-01-01", "period_end": "2031-12-31"}, "TDS Report")

def gstr1_page():
    st.title("GSTR1")
    if st.button("Request GSTR1 Report"):
        queue_report("/api/reports/gstr1", {"period_label": "Streamlit Run"}, "GSTR1 Report")

if not st.session_state.token:
    login_page()
else:
    with st.sidebar:
        st.write(f"Logged in as **{st.session_state.user_email}**")
        if st.button("Logout"):
            st.session_state.token = None
            st.session_state.user_email = None
            st.rerun()
            
        st.header("Navigation")
        page = st.radio("", ["Bank Rec", "Ledger", "Previous runs", "TDS", "GSTR1"])
        
    if page == "Bank Rec":
        bank_rec_page()
    elif page == "Ledger":
        ledger_page()
    elif page == "Previous runs":
        previous_runs_page()
    elif page == "TDS":
        tds_page()
    elif page == "GSTR1":
        gstr1_page()

# uv run streamlit run dev_dashboard.py
