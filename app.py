# app.py
import os
import io
import uuid
import re
from datetime import date, datetime
from typing import List, Optional, Dict, Tuple
import streamlit as st
from supabase import create_client, Client

# ─────────── Configuration générale ───────────
st.set_page_config(page_title="Suivi d’avancement", page_icon="📊", layout="wide")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
BUCKET_PV = os.getenv("PV_BUCKET", "pv-chantier")
FORCE_PUBLIC_URLS = os.getenv("FORCE_PUBLIC_URLS", "true").lower() in ("1", "true", "yes")

MAX_UPLOAD_MB = 200
ALLOWED_EXT = {".pdf", ".doc", ".docx"}

# ─────────── Fonctions utilitaires ───────────
def human_bytes(n: int) -> str:
    for u in ["B", "KB", "MB", "GB"]:
        if n < 1024.0:
            return f"{n:3.1f}{u}"
        n /= 1024.0
    return f"{n:.1f}TB"

def safe_filename(name: str) -> str:
    base = os.path.basename(name).replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)

def make_storage_path(project_id: str, d: date, original_name: str) -> str:
    ymd = d.strftime("%Y%m%d")
    return f"{project_id}/{ymd}/{uuid.uuid4().hex}_{safe_filename(original_name)}"

def dns_probe(url: str):
    import socket
    try:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        return socket.gethostbyname(host)
    except Exception:
        return None

def is_bucket_public(sb: Client, bucket: str) -> bool:
    return FORCE_PUBLIC_URLS

def to_public_url(sb: Client, bucket: str, path: str) -> str:
    return f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{bucket}/{path}"

def to_signed_url(sb: Client, bucket: str, path: str, expires=3600):
    try:
        signed = sb.storage.from_(bucket).create_signed_url(path, expires)
        return signed.get("signedURL", to_public_url(sb, bucket, path))
    except Exception:
        return to_public_url(sb, bucket, path)

# ─────────── Connexion Supabase ───────────
@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def test_connectivity_panel():
    with st.expander("Diagnostic rapide", expanded=False):
        ip = dns_probe(SUPABASE_URL) or "—"
        st.success(f"DNS OK → **{SUPABASE_URL.split('//')[-1]}** : {ip}")

# ─────────── Authentification ───────────
def login_panel(sb: Client):
    st.subheader("Connexion")
    mode = st.radio(" ", ["Se connecter", "Créer un compte"], horizontal=True, label_visibility="collapsed")
    email = st.text_input("Email", "", key="auth_email")
    pwd = st.text_input("Mot de passe", type="password", key="auth_pwd")
    user = st.session_state.get("user")
    col1, col2 = st.columns(2)

    if mode == "Se connecter" and col1.button("Connexion", type="primary"):
        try:
            res = sb.auth.sign_in_with_password({"email": email, "password": pwd})
            st.session_state["user"] = res.user
            st.success("Connecté.")
            st.rerun()
        except Exception as e:
            st.error(f"Connexion échouée : {e}")
    elif mode == "Créer un compte" and col1.button("Créer mon compte", type="primary"):
        try:
            res = sb.auth.sign_up({"email": email, "password": pwd})
            st.session_state["user"] = res.user
            st.success("Compte créé (vérifie ton email).")
            st.rerun()
        except Exception as e:
            st.error(f"Création échouée : {e}")

    if user:
        st.caption(f"Connecté : {getattr(user, 'email', '—')}")
        if col2.button("Se déconnecter"):
            sb.auth.sign_out()
            st.session_state.pop("user", None)
            st.rerun()
    return user

# ─────────── Liste des projets ───────────
def list_projects(sb: Client):
    try:
        res = sb.table("projects").select("id,name").order("name").execute()
        return res.data or []
    except Exception as e:
        st.warning(f"Erreur chargement projets : {e}")
        return []

# ─────────── Upload fichiers ───────────
def upload_pv_files(sb: Client, project_id: str, the_date: date, files):
    from_ = sb.storage.from_(BUCKET_PV)
    ok, rows = 0, []
    if not files:
        return ok, rows
    for f in files:
        name = getattr(f, "name", "file")
        if not any(name.lower().endswith(ext) for ext in ALLOWED_EXT):
            st.warning(f"Ignoré (extension non autorisée) : {name}")
            continue
        content = f.getvalue()
        if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
            st.warning(f"Ignoré (>{MAX_UPLOAD_MB} MB) : {name}")
            continue
        path = make_storage_path(project_id, the_date, name)
        try:
            from_.upload(path, content)
            rows.append({"path": path, "name": name})
            ok += 1
        except Exception as e:
            st.error(f"Erreur upload {name} : {e}")
    return ok, rows

# ─────────── Historique des PV ───────────
def storage_list_recursive(sb: Client, bucket: str, prefix: str):
    """Version robuste qui ignore les None."""
    out, stack = [], [prefix.rstrip("/") + "/"] if prefix else [""]
    from_ = sb.storage.from_(bucket)
    while stack:
        cur = stack.pop()
        try:
            entries = from_.list(cur) or []
        except Exception:
            entries = []
        for e in entries:
            if not isinstance(e, dict):  # ignore None
                continue
            e_type = e.get("type") or (e.get("metadata") or {}).get("type")
            name = e.get("name")
            if not name:
                continue
            full = (cur + name).lstrip("/")
            if e_type == "folder":
                stack.append(full + "/")
            else:
                e["full_path"] = full
                out.append(e)
    return out

def render_pv_history(sb: Client, project_id: str):
    st.markdown("### 📎 Pièces jointes — PV de chantier")
    try:
        entries = storage_list_recursive(sb, BUCKET_PV, project_id)
    except Exception as e:
        st.error(f"Erreur lecture Storage : {e}")
        return
    files = [e for e in entries if isinstance(e, dict) and (e.get("type") == "file" or e.get("name"))]
    if not files:
        st.info("Aucun PV pour ce projet.")
        return

    # Regroupement par date extraite du chemin
    groups = {}
    for e in files:
        path = e.get("full_path", "")
        parts = path.split("/")
        ymd = "inconnu"
        if len(parts) >= 2 and re.fullmatch(r"\d{8}", parts[1]):
            y, m, d = parts[1][:4], parts[1][4:6], parts[1][6:]
            ymd = f"{y}-{m}-{d}"
        groups.setdefault(ymd, []).append(e)

    for ymd in sorted(groups.keys(), reverse=True):
        st.markdown(f"**{ymd}**")
        for e in sorted(groups[ymd], key=lambda x: x.get("full_path", "")):
            path = e.get("full_path", "")
            fname = path.split("/", 3)[-1]
            url = to_public_url(sb, BUCKET_PV, path) if FORCE_PUBLIC_URLS else to_signed_url(sb, BUCKET_PV, path)
            st.write(f"- [{fname}]({url})")

# ─────────── Formulaire principal ───────────
def form_panel(sb: Client, projects):
    st.header("Suivi d’avancement — Saisie")
    if not projects:
        st.info("Aucun projet disponible.")
        return

    names = [p["name"] for p in projects]
    default = 0
    saved = st.session_state.get("selected_project_id")
    if saved:
        for i, p in enumerate(projects):
            if p["id"] == saved:
                default = i
                break
    name = st.selectbox("Projet", names, index=default)
    project_id = next(p["id"] for p in projects if p["name"] == name)
    st.session_state["selected_project_id"] = project_id

    with st.form("update_form"):
        col1, col2 = st.columns(2)
        with col1:
            t = st.number_input("Progression travaux (%)", 0.0, 100.0, 0.0)
        with col2:
            p = st.number_input("Progression paiements (%)", 0.0, 100.0, 0.0)
        d = st.date_input("Date du PV (optionnel)", value=None, format="YYYY/MM/DD")
        com = st.text_area("Commentaires", placeholder="Observations…")
        st.markdown("#### Joindre le PV (PDF/DOCX/DOC)")
        files = st.file_uploader(" ", type=["pdf", "doc", "docx"], accept_multiple_files=True, label_visibility="collapsed")
        sub = st.form_submit_button("Enregistrer la mise à jour", type="primary")

    if sub:
        user = st.session_state.get("user")
        if not user:
            st.error("Veuillez vous connecter.")
            return
        uid = getattr(user, "id", None)
        nb, _ = upload_pv_files(sb, project_id, d or date.today(), files)
        data = {
            "project_id": project_id,
            "updated_by": uid,
            "progress_travaux": float(t),
            "progress_paiements": float(p),
            "commentaires": com or "",
            "pv_chantier": d.isoformat() if isinstance(d, date) else None,
        }
        try:
            sb.table("project_updates").insert(data).execute()
            st.success(f"Mise à jour enregistrée. Fichiers déposés : {nb}")
        except Exception as e:
            st.error(f"Erreur base de données : {e}")

    st.divider()
    render_pv_history(sb, project_id)

# ─────────── Main ───────────
def main():
    try:
        sb = get_supabase()
    except Exception as e:
        st.error(f"Erreur connexion Supabase : {e}")
        return
    test_connectivity_panel()

    user = st.session_state.get("user")
    if not user:
        login_panel(sb)
        return
    st.caption(f"Connecté : {getattr(user, 'email', '—')}")
    if st.button("Se déconnecter"):
        sb.auth.sign_out()
        st.session_state.pop("user", None)
        st.rerun()

    projects = list_projects(sb)
    form_panel(sb, projects)

if __name__ == "__main__":
    main()
