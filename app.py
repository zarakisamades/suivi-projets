import streamlit as st
from supabase import create_client, Client
import socket
import httpx

st.set_page_config(page_title="Suivi d’avancement — Saisie hebdomadaire", page_icon="📊")

# Lecture des secrets Streamlit
SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_ANON_KEY = st.secrets["SUPABASE_ANON_KEY"]

# --- Vérification DNS ---
st.title("🧭 Test DNS Supabase")

try:
    host = SUPABASE_URL.replace("https://", "").replace("http://", "").split("/")[0]
    socket.gethostbyname(host)
    st.success(f"✅ DNS OK : {host}")
except Exception as e:
    st.error(f"❌ DNS échec : {e}")

# --- Vérification réseau ---
try:
    r = httpx.get(SUPABASE_URL + "/rest/v1", timeout=5)
    if r.status_code == 401:
        st.success("✅ Connexion réseau Supabase OK (status: 401)")
    else:
        st.warning(f"⚠️ Connexion réseau possible, code: {r.status_code}")
except Exception as e:
    st.error(f"❌ Erreur réseau vers Supabase : {e}")

# --- Initialisation Supabase ---
try:
    sb: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    st.success("✅ Import et initialisation de Supabase OK")
except Exception as e:
    st.error(f"❌ Erreur création client Supabase : {e}")

# --- Interface principale ---
st.header("📈 Suivi d’avancement — Saisie hebdomadaire")
menu = st.sidebar.radio("Connexion", ["Se connecter", "Créer un compte"])

email = st.text_input("Email", "")
password = st.text_input("Mot de passe (min 6 caractères)", type="password")

if menu == "Créer un compte":
    if st.button("Créer mon compte"):
        try:
            res = sb.auth.sign_up({"email": email, "password": password})
            if res.user:
                st.success("✅ Compte créé avec succès !")
            else:
                st.warning("⚠️ Échec création du compte : clé ou configuration invalide.")
        except Exception as e:
            st.error(f"❌ Erreur de création du compte : {e}")

elif menu == "Se connecter":
    if st.button("Connexion"):
        try:
            res = sb.auth.sign_in_with_password({"email": email, "password": password})
            if res.user:
                st.success(f"✅ Connecté en tant que {email}")
            else:
                st.warning("⚠️ Échec de connexion (vérifie tes identifiants).")
        except Exception as e:
            st.error(f"❌ Erreur de connexion : {e}")
