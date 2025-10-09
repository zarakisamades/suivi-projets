import streamlit as st
import socket

st.title("🔍 Test DNS Supabase")

host = "otlyezgdpkchuakjemf.supabase.co"
try:
    ip = socket.gethostbyname(host)
    st.success(f"✅ DNS OK : {host} → {ip}")
except Exception as e:
    st.error(f"❌ DNS échec : {e}")
