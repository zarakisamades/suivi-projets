# ---------- app.py (PARTIE A) ----------
import os, socket, traceback, platform, uuid
from datetime import date, datetime
from typing import Optional, List, Dict, Any

import streamlit as st
from supabase import create_client, Client

# ---- Etat global par défaut (doit être tout en haut, avant les widgets) ----
for k, v in {
    "user": None,                          # dict user supabase ou None
    "auth_panel_visible": True,            # pour masquer la colonne auth quand connecté
    "selected_project_id": None,           # id projet sélectionné
    "uploader_version": 0,                 # pour reset le file_uploader
    "form_progress_travaux": None,
    "form_progress_paiements": None,
    "form_date_pv": None,                  # type: Optional[date]
    "form_commentaires": "",
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ---- Config page ----
st.set_page_config(
    page_title="Suivi d’avancement — Saisie hebdomadaire",
    page_icon="📈",
    layout="wide"
)

# ---- Constantes ----
BUCKET_PV = "pv-chantier"
MAX_UPLOAD_MB = 25
ALLOWED_EXT = {".pdf", ".docx", ".doc"}

# ---- Lecture des secrets/env ----
SUPABASE_URL = os.getenv("SUPABASE_URL") or st.secrets.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or st.secrets.get("SUPABASE_ANON_KEY", "")

@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

sb: Client = get_supabase()

def dns_banner():
    """Bannière d’info DNS pour debug."""
    try:
        host = SUPABASE_URL.split("://", 1)[-1].split("/", 1)[0]
        ip = socket.gethostbyname(host)
        st.success(f"Connexion réseau Supabase OK (status attendu: 401) — DNS {host} → {ip}")
    except Exception:
        st.warning("DNS : échec de résolution du domaine Supabase.")
# ---------- fin PARTIE A ----------
# ---------- app.py (PARTIE B) ----------
# ---------- Helpers projets ----------

def fetch_projects() -> List[Dict[str, Any]]:
    """Retourne la liste des projets [{id, name}] triés par name."""
    res = sb.table("projects").select("id,name").order("name").execute()
    return res.data or []

def reset_form_state():
    """Vide tous les champs + reset file_uploader."""
    st.session_state["form_progress_travaux"] = None
    st.session_state["form_progress_paiements"] = None
    st.session_state["form_date_pv"] = None
    st.session_state["form_commentaires"] = ""
    st.session_state["uploader_version"] = st.session_state.get("uploader_version", 0) + 1

def on_project_change():
    """Callback au changement de projet : reset + pré-remplissage dernière MAJ (optionnel)."""
    reset_form_state()
    pid = st.session_state.get("selected_project_id")
    if not pid:
        return
    try:
        last = (
            sb.table("project_updates")
              .select("*")
              .eq("project_id", pid)
              .order("created_at", desc=True)
              .limit(1)
              .execute()
        )
        if last.data:
            row = last.data[0]
            st.session_state["form_progress_travaux"]  = row.get("progress_travaux")
            st.session_state["form_progress_paiements"] = row.get("progress_paiements")
            st.session_state["form_date_pv"]            = row.get("pv_chantier")
            st.session_state["form_commentaires"]       = row.get("commentaires") or ""
    except Exception:
        # Pas bloquant si pas d'historique
        pass

# ---------- Helpers Storage (PV) ----------

def _safe_ext(filename: str) -> str:
    fn = filename or ""
    dot = fn.rfind(".")
    return fn[dot:].lower() if dot != -1 else ""

def upload_pv(project_id: str, file) -> Optional[str]:
    """
    Upload du PV vers storage.
    - file est un st.uploaded_file
    Retourne le chemin dans le bucket, ou None si pas d’upload.
    """
    if not file:
        return None
    ext = _safe_ext(file.name)
    if ext not in ALLOWED_EXT:
        st.warning(f"Format non autorisé ({ext}). Formats acceptés : {', '.join(sorted(ALLOWED_EXT))}")
        return None

    # IMPORTANT : on envoie les *bytes*, pas un BytesIO
    content = file.getvalue()
    if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
        st.warning(f"Fichier trop volumineux (> {MAX_UPLOAD_MB} Mo).")
        return None

    today = date.today().isoformat()
    new_name = f"{uuid.uuid4().hex}_{file.name}"
    path = f"{project_id}/{today}/{new_name}"

    # supabase-py accepte bytes pour upload
    sb.storage.from_(BUCKET_PV).upload(path, content)
    return path

def list_signed_pv(project_id: str, expires_sec: int = 3600) -> List[Dict[str, Any]]:
    """
    Liste les fichiers du dossier du projet et renvoie des URLs signées.
    """
    try:
        # On liste récursivement par dates (sous-dossiers)
        base = f"{project_id}"
        entries = sb.storage.from_(BUCKET_PV).list(path=base, limit=1000)
        files = []
        for item in entries:
            if item.get("id") or item.get("name"):  # c'est un fichier dans base/
                key = f"{base}/{item['name']}"
                url = sb.storage.from_(BUCKET_PV).create_signed_url(key, expires_sec)
                files.append({"file_name": item["name"], "url": url["signedURL"], "uploaded_at": item.get("created_at")})
            # Si item est un "dossier", on re-liste :
            if item.get("type") == "folder":
                sub = sb.storage.from_(BUCKET_PV).list(path=f"{base}/{item['name']}", limit=1000)
                for s in sub:
                    key = f"{base}/{item['name']}/{s['name']}"
                    url = sb.storage.from_(BUCKET_PV).create_signed_url(key, expires_sec)
                    files.append({"file_name": f"{item['name']}/{s['name']}", "url": url["signedURL"], "uploaded_at": s.get("created_at")})
        # Tri grossier par nom (contient la date dans le chemin)
        files.sort(key=lambda x: x["file_name"], reverse=True)
        return files
    except Exception:
        return []

# ---------- Auth helpers ----------

def do_login(email: str, password: str) -> bool:
    try:
        res = sb.auth.sign_in_with_password({"email": email, "password": password})
        st.session_state["user"] = res.user.model_dump()
        st.session_state["auth_panel_visible"] = False
        return True
    except Exception as e:
        st.error("Échec de connexion. Vérifie l’email / mot de passe (ou la confirmation email).")
        return False

def do_logout():
    try:
        sb.auth.sign_out()
    except Exception:
        pass
    st.session_state["user"] = None
    st.session_state["auth_panel_visible"] = True

def do_signup(email: str, password: str):
    try:
        sb.auth.sign_up({"email": email, "password": password})
        st.success("Compte créé. Vérifie ta boîte mail pour confirmer l’adresse.")
    except Exception:
        st.error("Échec de création du compte.")
# ---------- fin PARTIE B ----------
# ---------- app.py (PARTIE C) ----------
def auth_panel():
    """Panneau d’auth : réduit quand connecté."""
    user = st.session_state.get("user")
    if user:
        st.write(f"Connecté : {user.get('email')}")
        if st.button("Se déconnecter"):
            do_logout()
        st.divider()
        return

    # Non connecté : mini form
    mode = st.radio("Connexion", ["Se connecter", "Créer un compte"], horizontal=False)
    email = st.text_input("Email", key="auth_email")
    pwd = st.text_input("Mot de passe", type="password", key="auth_pwd")

    colA, colB = st.columns(2)
    with colA:
        if st.button("Connexion"):
            if email and pwd:
                do_login(email, pwd)
    with colB:
        if st.button("Créer"):
            if email and pwd:
                do_signup(email, pwd)

def form_panel(projects: List[Dict[str, Any]]):
    """Formulaire de saisie + upload PV pour l’utilisateur connecté."""
    if not projects:
        st.info("Aucun projet trouvé dans la table projects.")
        return

    # --- Sélecteur de projet (avec callback) ---
    st.selectbox(
        "Projet",
        options=[p["id"] for p in projects],
        format_func=lambda pid: next(p["name"] for p in projects if p["id"] == pid),
        key="selected_project_id",
        on_change=on_project_change,
    )

    st.markdown("### Nouvelle mise à jour")

    # Widgets *liés* à session_state (pas de value=..., Streamlit utilisera le key)
    col1, col2 = st.columns(2)
    with col1:
        st.number_input(
            "Progression travaux (%)",
            min_value=0.0, max_value=100.0, step=1.0,
            key="form_progress_travaux",
        )
    with col2:
        st.number_input(
            "Progression paiements (%)",
            min_value=0.0, max_value=100.0, step=1.0,
            key="form_progress_paiements",
        )

    st.date_input(
        "Date du PV de chantier (optionnel)",
        key="form_date_pv"
    )

    st.text_area(
        "Commentaires",
        key="form_commentaires",
        placeholder="Observations, risques, points bloquants…"
    )

    # Uploader versionné (se vide quand on change de projet)
    uploaded = st.file_uploader(
        "Joindre le PV (PDF/DOCX/DOC)",
        type=[ext.strip(".") for ext in ALLOWED_EXT],
        key=f"pv_uploader_{st.session_state.get('uploader_version', 0)}",
    )

    # ----- Bouton d’enregistrement -----
    if st.button("Enregistrer la mise à jour", type="primary"):
        try:
            pid = st.session_state.get("selected_project_id")
            if not pid:
                st.warning("Choisis d’abord un projet.")
                return
            user = st.session_state.get("user") or {}
            user_id = user.get("id")

            # Upload PV (si fourni)
            pv_path = upload_pv(pid, uploaded) if uploaded else None

            # Insertion en base
            payload = {
                "project_id": pid,
                "updated_by": user_id,
                "progress_travaux": st.session_state.get("form_progress_travaux"),
                "progress_paiements": st.session_state.get("form_progress_paiements"),
                "pv_chantier": st.session_state.get("form_date_pv"),
                "commentaires": st.session_state.get("form_commentaires"),
            }
            sb.table("project_updates").insert(payload).execute()

            # Succès
            nfiles = 1 if pv_path else 0
            st.success(f"Mise à jour enregistrée. Fichiers déposés : {nfiles}")

            # Reset du formulaire après enregistrement
            reset_form_state()

        except Exception as e:
            st.error(f"Erreur enregistrement mise à jour : {e}")

    # ----- Section pièces jointes (liste PV) -----
    st.markdown("### Pièces jointes — PV de chantier")
    pid = st.session_state.get("selected_project_id")
    if pid:
        pv_list = list_signed_pv(pid, expires_sec=3600)
        if not pv_list:
            st.info("Aucun PV (nouveau flux) pour ce projet.")
        else:
            for item in pv_list:
                st.markdown(
                    f"- **[{item['file_name']}]({item['url']})**  "
                    f"_uploadé le : {item.get('uploaded_at', '')}_",
                    unsafe_allow_html=True
                )
# ---------- fin PARTIE C ----------
# ---------- app.py (PARTIE D) ----------
def main():
    st.title("Suivi d’avancement — Saisie")

    # Bannière DNS (debug)
    dns_banner()

    user = st.session_state.get("user")

    # Mise en page : 2 colonnes si panneau auth visible, sinon 1 grande colonne
    if st.session_state.get("auth_panel_visible", True):
        col_auth, col_app = st.columns([1, 2])
    else:
        col_auth, col_app = st.columns([0.2, 1.8])

    with col_auth:
        auth_panel()

    with col_app:
        # Si connecté, on affiche la saisie, sinon un message
        if not st.session_state.get("user"):
            st.info("Connecte-toi pour saisir une mise à jour.")
            return

        # Charger les projets et initialiser un choix si besoin
        projects = fetch_projects()
        if projects and not st.session_state.get("selected_project_id"):
            st.session_state["selected_project_id"] = projects[0]["id"]
            # Pré-remplir la première fois
            on_project_change()

        form_panel(projects)

if __name__ == "__main__":
    main()
# ---------- fin PARTIE D ----------
