# app.py — Partie 1/2

import os
import socket
import platform
import traceback
import uuid
import mimetypes
from datetime import datetime, date
from typing import List, Dict, Optional, Tuple

import streamlit as st
from supabase import create_client, Client


# ---------------- Config page ----------------
st.set_page_config(
    page_title="Suivi d’avancement — Saisie hebdomadaire",
    page_icon="🗂️",
    layout="wide",
)


# ---------------- Constantes ----------------
BUCKET_PV = "pv-chantier"          # bucket public (lecture libre)
MAX_UPLOAD_MB = 200
ALLOWED_EXT = {".pdf", ".docx", ".doc"}


# ---------------- Supabase ----------------
def get_env(name: str, default: str = "") -> str:
    val = os.getenv(name, default).strip()
    return val


@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    url = get_env("SUPABASE_URL")
    key = get_env("SUPABASE_ANON_KEY")
    if not url or not key:
        st.stop()
    return create_client(url, key)


# ---------------- Réseau / DNS ----------------
def dns_ok(hostname: str) -> Tuple[bool, str]:
    try:
        ip = socket.gethostbyname(hostname)
        return True, ip
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def render_dns_banner():
    url = get_env("SUPABASE_URL")
    host = ""
    try:
        # ex: https://kwgqaudyirdesedaxrld.supabase.co
        host = url.split("//", 1)[1].split("/", 1)[0]
    except Exception:
        pass

    ok, info = dns_ok(host) if host else (False, "URL Supabase invalide")
    if ok:
        st.success(
            f"Connexion réseau Supabase OK (status attendu: 401) — DNS {host} → {info}",
            icon="✅",
        )
    else:
        st.error(f"DNS échec : {info}", icon="❌")


# ---------------- Auth ----------------
def login_panel(sb: Client) -> Optional[Dict]:
    st.subheader("Connexion")
    mode = st.radio(
        " ",
        ["Se connecter", "Créer un compte"],
        horizontal=False,
        index=0,
        label_visibility="collapsed",
        key="auth_mode_radio",
    )

    email = st.text_input("Email", key="auth_email")
    password = st.text_input("Mot de passe", type="password", key="auth_pwd")

    colb1, colb2 = st.columns([1, 1])
    with colb1:
        if st.button("Connexion") and mode == "Se connecter":
            try:
                res = sb.auth.sign_in_with_password({"email": email, "password": password})
                st.session_state["user"] = res.user
            except Exception as e:
                st.error(f"Échec de connexion : {e}")
    with colb2:
        if st.button("Créer le compte") and mode == "Créer un compte":
            try:
                sb.auth.sign_up({"email": email, "password": password})
                st.info("Compte créé. Vérifie ton email, puis connecte-toi.")
            except Exception as e:
                st.error(f"Échec de création du compte : {e}")

    user = st.session_state.get("user")
    return user


def header_user(sb: Client, user: Dict):
    st.caption(f"Connecté : {user.get('email')}")
    if st.button("Se déconnecter"):
        try:
            sb.auth.sign_out()
        except Exception:
            pass
        st.session_state.pop("user", None)
        st.rerun()


# ---------------- Projets ----------------
def list_projects(sb: Client) -> List[Dict]:
    """Retourne les projets visibles pour l'utilisateur courant (table 'projects')."""
    try:
        res = sb.table("projects").select("id, name").order("name").execute()
        return res.data or []
    except Exception as e:
        st.warning(f"Aucun projet trouvé (ou erreur). Détails : {e}")
        return []


# ---------------- Fichiers / Storage ----------------
def _safe_filename(original_name: str) -> str:
    # Normalise le nom pour éviter caractères problématiques
    base = os.path.basename(original_name or "").replace(" ", "_")
    base = "".join(c for c in base if c.isalnum() or c in ("_", "-", ".", "(", ")"))
    if not base:
        base = "document"
    # préfix UUID pour éviter les collisions
    return f"{uuid.uuid4().hex}_{base}"


def _content_type_from_name(name: str) -> str:
    ct, _ = mimetypes.guess_type(name)
    return ct or "application/octet-stream"


def upload_pv_files(
    sb: Client,
    project_id: str,
    files: List["UploadedFile"],
    pv_date: Optional[date],
) -> Tuple[int, List[str]]:
    """
    Upload les fichiers vers: pv-chantier/<project_id>/<YYYYMMDD>/<filename>
    Retourne: (nb_upload_ok, messages)
    """
    msgs: List[str] = []
    if not files:
        return 0, msgs

    store = sb.storage.from_(BUCKET_PV)
    d_folder = (pv_date or date.today()).strftime("%Y%m%d")

    ok_count = 0
    for f in files:
        ext = os.path.splitext(f.name)[1].lower()
        if ext not in ALLOWED_EXT:
            msgs.append(f"⛔ {f.name} : extension non autorisée.")
            continue
        if f.size and (f.size > MAX_UPLOAD_MB * 1024 * 1024):
            msgs.append(f"⛔ {f.name} : taille > {MAX_UPLOAD_MB} MB.")
            continue

        safe_name = _safe_filename(f.name)
        obj_path = f"{project_id}/{d_folder}/{safe_name}"
        try:
            data = f.read()  # bytes
            ctype = _content_type_from_name(f.name)
            store.upload(
                obj_path,
                data,
                file_options={"content-type": ctype, "upsert": False},
            )
            msgs.append(f"✅ {f.name} déposé.")
            ok_count += 1
        except Exception as e:
            msgs.append(f"⚠️ {f.name} : upload échoué ({e})")

    return ok_count, msgs


def list_public_pv(sb: Client, project_id: str) -> List[Dict]:
    """
    Liste tous les PV pour un projet, renvoie:
    { 'date_folder': 'YYYYMMDD', 'file_name': 'xx.pdf', 'url': '<public-url>', 'uploaded_at': ISO }
    ATTENTION: bucket public requis (policy de lecture).
    """
    store = sb.storage.from_(BUCKET_PV)
    out: List[Dict] = []

    try:
        # 1) lister les dossiers dates : <project_id>/
        # la SDK retourne dossiers + fichiers; on va filtrer
        entries = store.list(project_id)
    except Exception as e:
        st.error(f"Erreur lecture Storage (racine projet): {e}")
        return out

    # Sous-dossiers = dates (heuristique: item sans 'id' ou avec 'metadata' vide, et / pas dans le nom)
    date_folders = [it["name"] for it in entries if "/" not in it.get("name", "")]
    # tri desc
    date_folders.sort(reverse=True)

    for folder in date_folders:
        prefix = f"{project_id}/{folder}"
        try:
            files = store.list(prefix)
        except Exception as e:
            st.warning(f"Erreur lecture Storage ({prefix}) : {e}")
            continue

        for item in files:
            name = item.get("name", "")
            if not name:
                continue
            obj_path = f"{prefix}/{name}"
            # URL publique permanente :
            pub = store.get_public_url(obj_path)
            url = pub.get("publicURL") or pub.get("public_url") or ""
            uploaded_at = (
                item.get("updated_at")
                or item.get("created_at")
                or datetime.utcnow().isoformat()
            )
            out.append(
                {
                    "date_folder": folder,
                    "file_name": name,
                    "url": url,
                    "uploaded_at": uploaded_at,
                }
            )

    # déjà trié par dossiers desc; on peut raffiner par uploaded_at si besoin
    return out
# app.py — Partie 2/2

# ---------------- Rendu Historique ----------------
def render_pv_history(sb: Client, project_id: Optional[str]):
    st.subheader("Pièces jointes — PV de chantier")
    if not project_id:
        st.info("Sélectionne un projet pour voir les PV.")
        return

    items = list_public_pv(sb, project_id)
    if not items:
        st.info("Aucun PV pour ce projet.")
        return

    # Groupement par date_folder
    by_date: Dict[str, List[Dict]] = {}
    for it in items:
        by_date.setdefault(it["date_folder"], []).append(it)

    # Affichage propre
    for d in sorted(by_date.keys(), reverse=True):
        st.markdown(f"### {d[:4]}-{d[4:6]}-{d[6:]}")
        for it in by_date[d]:
            nice_name = it["file_name"]
            url = it["url"]
            when = it["uploaded_at"]
            st.markdown(f"- [{nice_name}]({url}) — ajouté le `{when}`")


# ---------------- Enregistrement BDD ----------------
def insert_update_row(
    sb: Client,
    project_id: str,
    progress_travaux: float,
    progress_paiements: float,
    commentaires: str,
    pv_date: Optional[date],
) -> bool:
    """Insère une ligne dans project_updates (RLS doit permettre INSERT par l'utilisateur connecté)."""
    row = {
        "project_id": project_id,
        "progress_travaux": progress_travaux,
        "progress_paiments": progress_paiements,  # si ta colonne s'appelle progress_paiements, corrige ici
        "commentaires": commentaires,
        "pv_chantier": pv_date.isoformat() if pv_date else None,
    }
    try:
        sb.table("project_updates").insert(row).execute()
        return True
    except Exception as e:
        st.error(f"Erreur enregistrement mise à jour : {e}")
        return False


# ---------------- Panneau Formulaire ----------------
def form_panel(sb: Client, projects: List[Dict]):
    st.header("Suivi d’avancement — Saisie")

    if not projects:
        st.info("Aucun projet disponible.")
        return

    # Sélecteur projet
    project_names = [p["name"] for p in projects]
    project_ids = {p["name"]: p["id"] for p in projects}

    sel_name = st.selectbox("Projet", project_names, key="sel_project_name")
    project_id = project_ids.get(sel_name)

    st.subheader("Nouvelle mise à jour")

    # Champs de saisie
    col1, col2 = st.columns(2)
    with col1:
        progress_travaux = st.number_input(
            "Progression travaux (%)",
            min_value=0.0,
            max_value=100.0,
            step=1.0,
            value=0.0,
            key="form_progress_travaux",
        )
    with col2:
        progress_paiements = st.number_input(
            "Progression paiements (%)",
            min_value=0.0,
            max_value=100.0,
            step=1.0,
            value=0.0,
            key="form_progress_paiements",
        )

    pv_date: Optional[date] = st.date_input(
        "Date du PV de chantier (optionnel)",
        value=None,
        format="YYYY/MM/DD",
        key="form_date_pv",
    )
    commentaires = st.text_area(
        "Commentaires",
        placeholder="Observations, risques, points bloquants…",
        key="form_commentaires",
    )

    st.subheader("Joindre le PV (PDF/DOCX/DOC)")
    files = st.file_uploader(
        "Drag and drop files here",
        type=["pdf", "docx", "doc"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        key="form_files",
    )

    if st.button("Enregistrer la mise à jour", type="primary"):
        # 1) Enregistrer la ligne BDD (si RLS ok)
        ok_row = insert_update_row(
            sb,
            project_id,
            progress_travaux,
            progress_paiements,
            commentaires,
            pv_date,
        )

        # 2) Upload des fichiers
        ok_files, msgs = upload_pv_files(sb, project_id, files, pv_date)
        if msgs:
            with st.expander("Détails des fichiers déposés"):
                for m in msgs:
                    st.write(m)

        if ok_row:
            if ok_files > 0:
                st.success(f"Mise à jour enregistrée. Fichiers déposés : {ok_files}")
            else:
                st.success("Mise à jour enregistrée (aucun fichier déposé).")
        else:
            st.warning("La ligne BDD n'a pas été enregistrée (voir message d'erreur).")

    # Historique
    st.markdown("---")
    render_pv_history(sb, project_id)


# ---------------- Main ----------------
def main():
    render_dns_banner()
    sb = get_supabase()

    # Colonne gauche: Auth (compact) / droite: app
    col_auth, col_app = st.columns([1, 2], gap="large", vertical_alignment="top")

    with col_auth:
        user = st.session_state.get("user")
        if not user:
            user = login_panel(sb)
        else:
            header_user(sb, user)

    with col_app:
        # si pas loggé, on bloque la suite (RLS INSERT, etc.)
        user = st.session_state.get("user")
        if not user:
            st.info("Connecte-toi pour saisir une mise à jour.")
            return

        # liste projets
        projects = list_projects(sb)
        form_panel(sb, projects)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        st.error("Une erreur est survenue.")
        st.code(traceback.format_exc())
