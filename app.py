# app.py — Variante avec journal dans la table pv_files (Partie 1/2)

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
    base = os.path.basename(original_name or "").replace(" ", "_")
    base = "".join(c for c in base if c.isalnum() or c in ("_", "-", ".", "(", ")"))
    if not base:
        base = "document"
    return f"{uuid.uuid4().hex}_{base}"


def _content_type_from_name(name: str) -> str:
    ct, _ = mimetypes.guess_type(name)
    return ct or "application/octet-stream"


def get_public_url(sb: Client, obj_path: str) -> str:
    """Retourne l’URL publique permanente (bucket public requis)."""
    pub = sb.storage.from_(BUCKET_PV).get_public_url(obj_path)
    return pub.get("publicURL") or pub.get("public_url") or ""


def log_pv_file(
    sb: Client,
    project_id: str,
    file_name: str,
    obj_path: str,
    public_url: str,
    pv_date: Optional[date],
    uploaded_by: Optional[str],
) -> None:
    """Insère une ligne de journal dans pv_files."""
    row = {
        "project_id": project_id,
        "file_path": obj_path,
        "file_name": file_name,
        "public_url": public_url,
        "pv_date": pv_date.isoformat() if pv_date else None,
        "uploaded_by": uploaded_by,
    }
    sb.table("pv_files").insert(row).execute()


def upload_pv_files(
    sb: Client,
    project_id: str,
    files: List["UploadedFile"],
    pv_date: Optional[date],
    uploaded_by: Optional[str],
) -> Tuple[int, List[str]]:
    """
    Upload les fichiers vers: pv-chantier/<project_id>/<YYYYMMDD>/<filename>
    Journalise chaque upload dans la table pv_files.
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
            public_url = get_public_url(sb, obj_path)
            try:
                log_pv_file(
                    sb,
                    project_id=project_id,
                    file_name=safe_name,
                    obj_path=obj_path,
                    public_url=public_url,
                    pv_date=pv_date,
                    uploaded_by=uploaded_by,
                )
            except Exception as e_log:
                msgs.append(f"⚠️ Journalisation pv_files échouée: {e_log}")

            msgs.append(f"✅ {f.name} déposé.")
            ok_count += 1
        except Exception as e:
            msgs.append(f"⚠️ {f.name} : upload échoué ({e})")

    return ok_count, msgs
