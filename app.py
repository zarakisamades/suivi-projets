# app.py

import os, socket, traceback, platform
from datetime import datetime, date
from pathlib import Path
import uuid

import streamlit as st
from typing import Dict, Any, Optional, List, Tuple

# ---------------------- ÉTAT GLOBAL ----------------------
# (défini au tout début, avant tout usage des widgets)
for k, v in {
    "user": None,                   # objet user supabase
    "auth_panel_visible": True,     # panneau de login visible tant qu’on n’est pas loggé
    "selected_project_id": None,    # projet sélectionné
    "uploader_version": 0,          # pour vider le file_uploader après succès
}.items():
    st.session_state.setdefault(k, v)

def reset_form_state():
    """Nettoie toutes les clés de formulaire AVANT de dessiner les widgets."""
    for k in [
        "form_progress_travaux",
        "form_progress_paiements",
        "form_date_pv",
        "form_commentaires",
        # uploader: on ne pop pas la clé dynamique (on change de version)
    ]:
        st.session_state.pop(k, None)

# ---------------------- CONFIG PAGE ----------------------
st.set_page_config(
    page_title="Suivi d’avancement — Saisie hebdomadaire",
    page_icon="📊",
    layout="wide"
)

# ---------------------- CONSTANTES -----------------------
BUCKET_PV = "pv-chantier"
MAX_UPLOAD_MB = 25
ALLOWED_EXT = {".pdf", ".docx", ".doc"}

# ---------------------- SUPABASE -------------------------
from supabase import create_client, Client

def get_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Variable d'environnement manquante: {name}")
    return v

@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    url = get_env("SUPABASE_URL")
    key = get_env("SUPABASE_ANON_KEY")
    return create_client(url, key)

def dns_ok(host: str) -> Tuple[bool, str]:
    try:
        ip = socket.gethostbyname(host)
        return True, ip
    except Exception as e:
        return False, str(e)

sb: Client = get_supabase()

def info_supabase_health():
    url = os.environ.get("SUPABASE_URL", "")
    host = url.replace("https://", "").split("/")[0]
    ok, detail = dns_ok(host)
    if ok:
        st.success(f"Connexion réseau Supabase OK (status attendu: 401) — DNS {host} → {detail}")
    else:
        st.error(f"DNS échec : {detail}")

# ---------------------- DATA ACCESS ----------------------
def fetch_projects(sb: Client) -> List[Dict[str, Any]]:
    """Retourne [{id, name}]"""
    res = sb.table("projects").select("id,name").order("name").execute()
    rows = res.data or []
    return rows

def insert_project_update(
    sb: Client,
    project_id: str,
    user_id: str,
    progress_travaux: float,
    progress_paiements: float,
    date_pv: Optional[date],
    commentaires: str,
) -> str:
    payload = {
        "id": str(uuid.uuid4()),
        "project_id": project_id,
        "updated_by": user_id,
        "progress_travaux": float(progress_travaux) if progress_travaux is not None else None,
        "progress_paiements": float(progress_paiements) if progress_paiements is not None else None,
        "pv_chantier": date_pv.isoformat() if date_pv else None,
        "commentaires": commentaires or "",
        "created_at": datetime.utcnow().isoformat()
    }
    res = sb.table("project_updates").insert(payload).execute()
    if isinstance(res.data, list) and len(res.data) == 1:
        return res.data[0]["id"]
    # si le SDK ne renvoie rien, on renvoie l’ID qu’on a généré
    return payload["id"]

def list_signed_pv(sb: Client, project_id: str, expires_sec: int = 3600) -> List[Dict[str, Any]]:
    """Liste des fichiers PV (liens signés) pour un projet."""
    prefix = f"{project_id}/"
    try:
        files = sb.storage.from_(BUCKET_PV).list(prefix=prefix)
    except Exception:
        files = []

    items: List[Dict[str, Any]] = []
    for f in files or []:
        fname = f.get("name") or f.get("Key") or ""
        if not fname:
            continue
        full_path = f"{prefix}{fname}"
        try:
            signed = sb.storage.from_(BUCKET_PV).create_signed_url(full_path, expires_sec)
            url = signed.get("signedURL") or signed.get("signed_url") or signed
            items.append({
                "file_name": fname,
                "url": url,
                "uploaded_at": f.get("updated_at") or f.get("last_modified") or "",
            })
        except Exception:
            pass
    # tri inverse par date si possible, sinon par nom
    items.sort(key=lambda x: (x.get("uploaded_at") or "", x["file_name"]), reverse=True)
    return items

def upload_pv_files(sb: Client, bucket: str, project_id: str, uploaded_files: List[Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Retourne (uploaded, warnings)
    uploaded: [{ "path": "...", "file_name": "...", "size": int }]
    warnings: [ "message", ... ]
    """
    if not uploaded_files:
        return [], []

    uploaded, warnings = [], []
    today = datetime.utcnow().date().isoformat()

    for uf in uploaded_files:
        try:
            name = uf.name
            ext = Path(name).suffix.lower()
            if ext not in ALLOWED_EXT:
                warnings.append(f"{name}: extension non autorisée.")
                continue

            # LIRE LES BYTES depuis le widget (clé pour éviter l'erreur BytesIO)
            file_bytes = uf.read()
            if not file_bytes:
                warnings.append(f"{name}: fichier vide.")
                continue

            safe_name = name.replace("/", "_").replace("\\", "_")
            storage_key = f"{project_id}/{today}_{safe_name}"

            # Upload : la v2 accepte des bytes directement
            resp = sb.storage.from_(bucket).upload(storage_key, file_bytes)

            # Adapter ce test si votre SDK renvoie différemment
            if isinstance(resp, dict) and resp.get("error"):
                warnings.append(f"{name}: {resp['error']['message']}")
                continue

            uploaded.append({
                "path": storage_key,
                "file_name": name,
                "size": len(file_bytes),
            })

        except Exception as e:
            warnings.append(f"{name}: {e}")

    return uploaded, warnings
# ---------------------- UI AUTH --------------------------
def ui_auth_panel():
    st.subheader("Connexion")
    mode_create = st.radio("",
        options=["Se connecter", "Créer un compte"],
        index=0, horizontal=False, label_visibility="collapsed"
    )

    email = st.text_input("Email", key="auth_email")
    pwd = st.text_input("Mot de passe", type="password", key="auth_pwd")

    colA, colB = st.columns(2)
    with colA:
        if st.button("Connexion", use_container_width=True):
            try:
                res = sb.auth.sign_in_with_password({"email": email, "password": pwd})
                user = res.user
                if user:
                    st.session_state["user"] = {"id": user.id, "email": user.email}
                    st.session_state["auth_panel_visible"] = False   # masque le panneau après login
                    st.success(f"Connecté : {user.email}")
                    st.rerun()
            except Exception as e:
                st.error(f"Échec de connexion : {e}")

    with colB:
        if st.button("Se déconnecter", use_container_width=True):
            try:
                sb.auth.sign_out()
            except Exception:
                pass
            st.session_state["user"] = None
            st.session_state["auth_panel_visible"] = True
            st.rerun()

    if mode_create == "Créer un compte":
        st.info("Créez un compte depuis l’onglet 'Créer un compte' si vous avez activé l’inscription côté Supabase.")

# --------------- Sélection de projet ---------------------
def on_project_change():
    # Nettoie l'état AVANT de redessiner les widgets + vide l’uploader
    reset_form_state()
    st.session_state["uploader_version"] = st.session_state.get("uploader_version", 0) + 1
    st.rerun()

def ui_project_selector(projects: List[Dict[str, Any]]):
    if not projects:
        st.warning("Aucun projet trouvé dans la table projects.")
        return None, {}
    project_name_by_id = {p["id"]: p["name"] for p in projects}
    options = list(project_name_by_id.keys())

    # sélection initiale si nécessaire
    if st.session_state.get("selected_project_id") not in options:
        st.session_state["selected_project_id"] = options[0]

    pid = st.selectbox(
        "Projet",
        options=options,
        index=options.index(st.session_state["selected_project_id"]),
        format_func=lambda x: project_name_by_id[x],
        key="selected_project_id",
        on_change=on_project_change
    )
    return pid, project_name_by_id

# ---------------------- FORMULAIRE ------------------------
def ui_update_form(project_id: str):
    st.subheader("Nouvelle mise à jour")

    col1, col2 = st.columns(2)
    with col1:
        progress_travaux = st.number_input(
            "Progression travaux (%)",
            min_value=0.0, max_value=100.0, step=1.0,
            key="form_progress_travaux",
            help="Pourcentage global d’avancement technique"
        )
    with col2:
        progress_paiements = st.number_input(
            "Progression paiements (%)",
            min_value=0.0, max_value=100.0, step=1.0,
            key="form_progress_paiements",
            help="Pourcentage global d’avancement financier"
        )

    date_pv = st.date_input(
        "Date du PV de chantier (optionnel)",
        key="form_date_pv"
    )

    commentaires = st.text_area(
        "Commentaires",
        key="form_commentaires",
        placeholder="Observations, risques, points bloquants…"
    )

    # Uploader avec clé versionnée pour reset après succès
    key_uploader = f"form_pv_files_v{st.session_state.get('uploader_version', 0)}"
    uploaded_files = st.file_uploader(
        "Joindre le PV (PDF/DOCX/DOC)",
        type=["pdf", "docx", "doc"],
        accept_multiple_files=True,
        key=key_uploader
    )

    col_btn = st.container()
    with col_btn:
        if st.button("Enregistrer la mise à jour", type="primary"):
            # Récupérer l’utilisateur
            user = st.session_state.get("user")
            if not user:
                st.error("Vous devez être connecté pour enregistrer une mise à jour.")
                return

            try:
                # 1) Upload des fichiers
                uploaded, warns = upload_pv_files(
                    sb, BUCKET_PV, project_id, uploaded_files or []
                )
                if warns:
                    st.warning("Quelques fichiers n’ont pas été pris en compte :\n- " + "\n- ".join(warns))

                # 2) Insertion en base
                update_id = insert_project_update(
                    sb,
                    project_id=project_id,
                    user_id=user["id"],
                    progress_travaux=progress_travaux,
                    progress_paiements=progress_paiements,
                    date_pv=date_pv if isinstance(date_pv, date) else None,
                    commentaires=commentaires,
                )

                st.success(f"Mise à jour enregistrée. Fichiers déposés : {len(uploaded)}")

                # 3) Reset de l’uploader et des champs (pour repartir propre)
                reset_form_state()
                st.session_state["uploader_version"] = st.session_state.get("uploader_version", 0) + 1
                st.rerun()

            except Exception as e:
                st.error(f"Erreur enregistrement mise à jour : {e}")

    # Liste des PV déposés (nouveau flux)
    st.subheader("Pièces jointes — PV de chantier")
    pv_list = list_signed_pv(sb, project_id, expires_sec=3600)
    if not pv_list:
        st.info("Aucun PV (nouveau flux) pour ce projet.")
    else:
        for item in pv_list:
            st.markdown(
                f"- **[{item['file_name']}]({item['url']})**  \n"
                f"  _Uploadé le : {item.get('uploaded_at','')}_",
                unsafe_allow_html=True
            )
# ---------------------- MAIN -----------------------------
def main():
    info_supabase_health()

    # Layout principal : 2 colonnes (à gauche auth, à droite appli)
    col_auth, col_app = st.columns(2)

    with col_auth:
        # Panneau de connexion masqué après login
        if st.session_state.get("auth_panel_visible", True) or not st.session_state.get("user"):
            ui_auth_panel()
        else:
            st.caption(f"Connecté : **{st.session_state['user']['email']}**")
            # bouton rapide de logout
            if st.button("Se déconnecter"):
                try:
                    sb.auth.sign_out()
                except Exception:
                    pass
                st.session_state["user"] = None
                st.session_state["auth_panel_visible"] = True
                st.rerun()

    with col_app:
        st.header("Suivi d’avancement — Saisie")

        # Si pas de user, on bloque la saisie
        if not st.session_state.get("user"):
            st.info("Connectez-vous pour saisir une mise à jour.")
            return

        # Charger les projets
        try:
            projects = fetch_projects(sb)
        except Exception as e:
            st.error(f"Erreur lecture projets : {e}")
            projects = []

        project_id, project_names = ui_project_selector(projects)
        if not project_id:
            return

        # Formulaire principal (sans réécriture tardive de session_state)
        ui_update_form(project_id)

# ---------------------- LANCEMENT -------------------------
if __name__ == "__main__":
    try:
        main()
    except Exception:
        st.error("Une erreur s’est produite. Détails dans les logs.")
        st.exception(traceback.format_exc())
