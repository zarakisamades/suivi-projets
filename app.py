import json
import os
from datetime import date

import pandas as pd
import streamlit as st
from supabase import create_client, Client

# ----------------------
# Config de base
# ----------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise RuntimeError(
            "Variables d’environnement SUPABASE_URL / SUPABASE_ANON_KEY manquantes.\n"
            "👉 Sur Render: Settings → Environment → ajoute SUPABASE_URL et SUPABASE_ANON_KEY."
        )
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

sb: Client = get_supabase()
st.set_page_config(page_title="Suivi d’avancement", page_icon="📈", layout="centered")

# ----------------------
# Helpers Auth
# ----------------------
if "user" not in st.session_state:
    st.session_state.user = None

def sign_in(email: str, password: str):
    try:
        res = sb.auth.sign_in_with_password({"email": email, "password": password})
        st.session_state.user = res.user
        st.toast("Connecté ✅")
    except Exception as e:
        st.error(f"Échec de connexion: {e}")

def sign_up(email: str, password: str):
    try:
        sb.auth.sign_up({"email": email, "password": password})
        st.info("Compte créé. Vérifie ton email si nécessaire, puis connecte-toi.")
    except Exception as e:
        st.error(f"Échec de création: {e}")

def sign_out():
    try:
        sb.auth.sign_out()
    finally:
        st.session_state.user = None
        st.toast("Déconnecté")

# ----------------------
# UI Auth (sidebar)
# ----------------------
with st.sidebar:
    st.markdown("### 🔐 Connexion")
    if st.session_state.user:
        st.success(f"Connecté: {st.session_state.user.email}")
        if st.button("Se déconnecter"):
            sign_out()
    else:
        tab_login, tab_signup = st.tabs(["Se connecter", "Créer un compte"])
        with tab_login:
            email = st.text_input("Email", key="login_email")
            pwd = st.text_input("Mot de passe", type="password", key="login_pwd")
            if st.button("Connexion"):
                sign_in(email, pwd)
        with tab_signup:
            email2 = st.text_input("Email (inscription)", key="signup_email")
            pwd2 = st.text_input("Mot de passe (min 6)", type="password", key="signup_pwd")
            if st.button("Créer mon compte"):
                sign_up(email2, pwd2)

# Si pas connecté → écran d’accueil
if not st.session_state.user:
    st.title("📊 Suivi d’avancement — Saisie hebdomadaire")
    st.write("Connecte-toi pour saisir une mise à jour.")
    st.stop()

# ----------------------
# Chargement des données de référence
# ----------------------
@st.cache_data(ttl=120)
def load_projects():
    resp = sb.table("projects").select("id,name,code").order("name").execute()
    return resp.data or []

@st.cache_data(ttl=60)
def load_editable_fields():
    # Les champs sont rendus dynamiquement par ordre
    resp = sb.table("editable_fields").select(
        "key,label,field_type,min_value,max_value,options,required,role_min,order_index"
    ).order("order_index").execute()
    return resp.data or []

projects = load_projects()
fields = load_editable_fields()

if not projects:
    st.warning("Aucun projet trouvé. Importez d’abord la table `projects` dans Supabase.")
    st.stop()

# ----------------------
# Formulaire dynamique
# ----------------------
st.title("📝 Nouvelle mise à jour")

# Sélecteur Projet
proj_names = [f"{p['name']} ({p.get('code') or ''})".strip() for p in projects]
proj_idx = st.selectbox(
    "Projet",
    options=range(len(projects)),
    format_func=lambda i: proj_names[i] if projects else "",
)

# Les clés “fixes” stockées en colonnes dédiées dans project_updates
fixed_keys = {"progress_travaux", "progress_paiements", "pv_chantier", "commentaires"}

payload = {}
extras = {}

with st.form("update_form", clear_on_submit=True):
    # Champs dynamiques selon editable_fields
    for f in fields:
        key = f["key"]
        label = f["label"]
        ftype = f["field_type"]
        minv = f.get("min_value")
        maxv = f.get("max_value")
        options = f.get("options")
        required = f.get("required", False)

        value = None
        if ftype == "number":
            # Streamlit exige des floats; on cast côté DB si nécessaire
            value = st.number_input(
                label,
                min_value=float(minv) if minv is not None else None,
                max_value=float(maxv) if maxv is not None else None,
                step=1.0,
            )
        elif ftype == "text":
            value = st.text_area(label)
        elif ftype == "date":
            d = st.date_input(label, value=date.today())
            value = d.isoformat()
        elif ftype == "select":
            opts = []
            if isinstance(options, dict) and "values" in options:
                opts = options["values"]
            elif isinstance(options, list):
                opts = options
            value = st.selectbox(label, options=opts) if opts else st.text_input(label)
        elif ftype == "boolean":
            value = st.checkbox(label)
        else:
            value = st.text_input(label)

        # Répartition payload vs extras
        if key in fixed_keys:
            payload[key] = value
        else:
            extras[key] = value

    submitted = st.form_submit_button("Enregistrer la mise à jour ✅")

if submitted:
    project_id = projects[proj_idx]["id"]
    user = st.session_state.user
    data = {
        "project_id": project_id,
        "updated_by": user.id,  # RLS: insert autorisé uniquement si updated_by = auth.uid()
        **payload,
        "extras": extras,
    }
    try:
        sb.table("project_updates").insert(data).execute()
        st.success("Mise à jour enregistrée !")
        st.cache_data.clear()  # rafraîchir caches si besoin
    except Exception as e:
        st.error(f"Erreur lors de l’enregistrement: {e}")

# ----------------------
# Historique récent
# ----------------------
st.markdown("---")
st.subheader("🗂️ Dernières mises à jour")

updates = (
    sb.table("project_updates")
    .select("id,created_at,project_id,progress_travaux,progress_paiements,pv_chantier,commentaires,extras")
    .order("created_at", desc=True)
    .limit(50)
    .execute()
    .data
    or []
)

proj_by_id = {p["id"]: p for p in projects}

rows = []
for u in updates:
    p = proj_by_id.get(u["project_id"], {})
    rows.append({
        "Projet": p.get("name"),
        "Code": p.get("code"),
        "Date": u.get("created_at"),
        "Avanc. travaux (%)": u.get("progress_travaux"),
        "Avanc. paiements (%)": u.get("progress_paiements"),
        "PV chantier": u.get("pv_chantier"),
        "Commentaires": u.get("commentaires"),
        "Extras": json.dumps(u.get("extras") or {}, ensure_ascii=False),
    })

df = pd.DataFrame(rows)
if not df.empty:
    st.dataframe(df, use_container_width=True)
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("Télécharger CSV", data=csv, file_name="updates_recent.csv", mime="text/csv")
else:
    st.info("Aucune mise à jour enregistrée pour le moment.")
