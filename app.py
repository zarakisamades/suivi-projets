import os, socket, traceback, platform
import streamlit as st

st.set_page_config(page_title="Healthcheck", page_icon="🩺")
st.title("🩺 Healthcheck Streamlit")

# 1) Versions clés (Python & libs)
st.write("Python:", platform.python_version())
try:
    import numpy, pandas, httpx
    st.write("numpy:", numpy.__version__, "pandas:", pandas.__version__, "httpx:", httpx.__version__)
except Exception as e:
    st.error(f"Import numpy/pandas/httpx KO: {e}")
    st.code(traceback.format_exc())

# 2) Secrets présents ?
url = os.getenv("SUPABASE_URL", "")
key = os.getenv("SUPABASE_ANON_KEY", "")
st.write("SUPABASE_URL:", (url[:50] + "...") if url else "❌ manquant")
st.write("SUPABASE_ANON_KEY:", "✅ présent" if key else "❌ manquant")

# 3) DNS + reachability Supabase
if url:
    try:
        host = url.replace("https://","").replace("http://","").split("/")[0]
        st.write("DNS:", host, "→", socket.gethostbyname(host))
        import httpx
        r = httpx.get(url + "/auth/v1/health", timeout=10.0)
        st.write("Auth health:", r.status_code, r.text[:120])
    except Exception as e:
        st.error(f"Network check error: {e}")
        st.code(traceback.format_exc())

# 4) Import du client Supabase
try:
    from supabase import create_client, Client
    st.success("Import supabase OK ✅")
except Exception as e:
    st.error(f"Import supabase KO ❌ : {e}")
    st.code(traceback.format_exc())
