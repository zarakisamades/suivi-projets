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
# 3) DNS + reachability Supabase
if url:
    import httpx
    try:
        r = httpx.get(url + "/rest/v1/", timeout=5.0)
        st.success(f"Connexion réseau Supabase OK ✅ (status: {r.status_code})")
    except Exception as e:
        st.error(f"Erreur réseau vers Supabase : {e}")


# 4) Import du client Supabase
try:
    from supabase import create_client, Client
    st.success("Import supabase OK ✅")
except Exception as e:
    st.error(f"Import supabase KO ❌ : {e}")
    st.code(traceback.format_exc())
