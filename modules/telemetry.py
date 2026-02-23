from supabase import create_client, Client
import streamlit as st
from datetime import datetime, timezone

def log_action(operator_id, lat, lon, icao, action):
    """Silently logs user actions to the Supabase database."""
    try:
        url: str = st.secrets["supabase"]["url"]
        key: str = st.secrets["supabase"]["key"]
        supabase: Client = create_client(url, key)
        
        data = {
            "operator_id": operator_id,
            "latitude": lat,
            "longitude": lon,
            "icao": icao,
            "action": action,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        supabase.table("telemetry_logs").insert(data).execute()
    except Exception as e:
        # Fails silently so the dashboard doesn't crash for the pilot if the DB is down
        pass
