# app.py
import os
import io
import uuid
import re
from datetime import date, datetime
from typing import List, Optional, Dict, Tuple

import streamlit as st
from supabase import create_client, Client

# ------------- Configuration de la page -------------
st.set_page_config(
    page_title="Suivi d’avancement — Saisie hebdomadaire",
    page_icon="📈",
    layout="wide",
)

# ------------- Constantes / ENV -------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
BUCKET_PV = os.getenv("PV_BUCKET", "pv-chantier")
# Si ton bucket est public et que tu veux des URLs permanentes :
FORCE_PUBLIC_URLS = os.getenv("FORCE_PUBLIC_URLS", "true").lower() in ("1", "true", "yes")

MAX_UPLOAD_MB = 200
ALLOWED_EXT = {".pdf", ".doc", ".docx"}

# ------------- Utilitaires -------------
def human_bytes(n: int) -> str:
    for u in ["B", "KB", "MB", "GB"]:
        if n < 1024.0:
            return f"{n:3.1f}{u}"
        n /= 1024.0
    return f"{n:.1f}TB"

def dns_probe(url: str) -> Optional[str]:
    import socket
    try:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        ip = socket.gethostbyname(host)
        return ip
    except Exception:
        return None

def safe_filename(name: str) -> str:
    base = os.path.basename(name).replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)

def make_storage_path(project_id: str, d: date, original_name: str) -> str:
    ymd = d.strftime("%Y%m%d")
    return f"{project_id}/{ymd}/{uuid.uuid4().hex}_{safe_filename(original_name)}"

def is_bucket_public(sb: Client, bucket: str) -> bool:
    # Pas d'API directe fiable dans le client sync → on obéit à FORCE_PUBLIC_URLS
    return FORCE_PUBLIC_URLS

def to_public_url(sb: Client, bucket: str, storage_path: str) -> str:
    base = SUPABASE_URL.rstrip("/")
    return f"{base}/storage/v1/object/public/{bucket}/{storage_path}"

def to_signed_url(sb: Client, bucket: str, storage_path: str, expires: int = 3600) -> str:
    try:
        signed = sb.storage.from_(bucket).create_signed_url(storage_path, expires)
        return signed["signedURL"] if isinstance(signed, dict) else signed
    except Exception:
        return to_public_url(sb, bucket, storage_path)

def storage_list_recursive(sb: Client, bucket: str, prefix: str) -> List[Dict]:
    out = []
    stack = [prefix.rstrip("/") + "/"] if prefix else [""]
    from_ = sb.storage.from_(bucket)
    while stack:
        cur = stack.pop()
        try:
            entries = from_.list(cur)
        except Exception:
            entries = []
        for e in entries:
            e_type = e.get("type") or e.get("metadata", {}).get("type")
            name = e.get("name", "")
            full = (cur + name).lstrip("/")
            if e_type == "folder":
                stack.append(full + "/")
            else:
                e["full_path"] = full
                out.append(e)
    return out

# ------------- Supabase -------------
@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Variables d’environnement SUPABASE_URL / SUPABASE_ANON_KEY manquantes.")
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def test_connectivity_panel():
    with st.expander("Diagnostic rapide", expanded=False):
        ip = dns_probe(SUPABASE_URL) or "—"
        st.success(f"DNS OK → **{SUPABASE_URL.split('//')[-1]}** : {ip}")

# ------------- Auth -------------
def login_panel(sb: Client) -> Optional[object]:
    st.subheader("Connexion")
    mode = st.radio(" ", ["Se connecter", "Créer un compte"], horizontal=True, label_visibility="collapsed")
    email = st.text_input("Email", value="", key="auth_email")
    pwd = st.text_input("Mot de passe", type="password", key="auth_pwd")

    colb1, colb2 = st.columns(2)
    user = st.session_state.get("user")

    if mode == "Se connecter":
        if colb1.button("Connexion", type="primary"):
            try:
                res = sb.auth.sign_in_with_password({"email": email, "password": pwd})
                user = res.user
                st.session_state["user"] = user
                st.success("Connecté.")
                st.rerun()
            except Exception as e:
                st.error(f"Échec connexion : {e}")
    else:
        if colb1.button("Créer mon compte", type="primary"):
            try:
                res = sb.auth.sign_up({"email": email, "password": pwd})
                user = res.user
                st.session_state["user"] = user
                st.success("Compte créé (vérifie ton email si la confirmation est activée).")
                st.rerun()
            except Exception as e:
                st.error(f"Échec création : {e}")

    if user:
        st.caption(f"Connecté : {getattr(user, 'email', '—')}")
        if colb2.button("Se déconnecter"):
            try:
                sb.auth.sign_out()
            except Exception:
                pass
            st.session_state.pop("user", None)
            st.rerun()
    return user

def header_user(sb: Client, user: object):
    """En-tête compact quand l’utilisateur est déjà connecté."""
    left, right = st.columns([1, 3])
    with left:
        st.caption(f"Connecté : {getattr(user, 'email', '—')}")
        if st.button("Se déconnecter"):
            try:
                sb.auth.sign_out()
            except Exception:
                pass
            st.session_state.pop("user", None)
            st.rerun()
    with right:
        pass

# ------------- Données / Projets -------------
def list_projects(sb: Client) -> List[Dict]:
    try:
        res = sb.table("projects").select("id,name").order("name").execute()
        return res.data or []
    except Exception as e:
        st.warning(f"Aucun projet trouvé (ou erreur) : {e}")
        return []

# ------------- Upload PV -------------
def upload_pv_files(
    sb: Client, project_id: str, the_date: date, files: List[object], bucket: str = BUCKET_PV
) -> Tuple[int, List[Dict]]:
    ok = 0
    rows = []
    if not files:
        return ok, rows
    from_ = sb.storage.from_(bucket)
    for f in files:
        name = getattr(f, "name", "file")
        if not any(name.lower().endswith(ext) for ext in ALLOWED_EXT):
            st.warning(f"Fichier ignoré (extension non autorisée) : {name}")
            continue
        content = f.getvalue()
        if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
            st.warning(f"Fichier ignoré (taille > {MAX_UPLOAD_MB}MB) : {name}")
            continue
        storage_path = make_storage_path(project_id, the_date, name)
        try:
            from_.upload(storage_path, content)
            ok += 1
            rows.append(
                {
                    "path": storage_path,
                    "name": name,
                    "uploaded_at": datetime.utcnow().isoformat() + "Z",
                }
            )
        except Exception as e:
            st.error(f"Échec upload {name} : {e}")
    return ok, rows

# ------------- Historique PV -------------
def render_pv_history(sb: Client, project_id: str):
    st.markdown("### 📎 Pièces jointes — PV de chantier")
    try:
        entries = storage_list_recursive(sb, BUCKET_PV, project_id)
    except Exception as e:
        st.error(f"Erreur lecture Storage : {e}")
        return
    files = [e for e in entries if (e.get("metadata") or {}).get("size", 0) > 0 or e.get("type") == "file"]
    if not files:
        st.info("Aucun PV pour ce projet.")
        return

    groups: Dict[str, List[Dict]] = {}
    for e in files:
        p = e.get("full_path", "")
        parts = p.split("/")
        ymd = "inconnu"
        if len(parts) >= 2 and re.fullmatch(r"\d{8}", parts[1]):
            y, m, d = parts[1][:4], parts[1][4:6], parts[1][6:]
            ymd = f"{y}-{m}-{d}"
        groups.setdefault(ymd, []).append(e)

    def sort_key(k: str) -> Tuple[int, str]:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", k):
            return (0, k)
        return (1, k)

    for ymd in sorted(groups.keys(), key=sort_key, reverse=True):
        st.markdown(f"**{ymd}**")
        for e in sorted(groups[ymd], key=lambda x: x.get("full_path", "")):
            storage_path = e.get("full_path", "")
            fname = storage_path.split("/", 3)[-1] if "/" in storage_path else storage_path
            if FORCE_PUBLIC_URLS and is_bucket_public(sb, BUCKET_PV):
                url = to_public_url(sb, BUCKET_PV, storage_path)
            else:
                url = to_signed_url(sb, BUCKET_PV, storage_path, expires=3600)
            size = e.get("metadata", {}).get("size")
            size_txt = f" — {human_bytes(size)}" if size else ""
            st.write(f"- [{fname}]({url}){size_txt}")

# ------------- Formulaire de saisie -------------
def form_panel(sb: Client, projects: List[Dict]):
    st.header("Suivi d’avancement — Saisie")
    if not projects:
        st.info("Aucun projet à afficher.")
        return

    proj_names = [p["name"] for p in projects]
    default_idx = 0
    saved_id = st.session_state.get("selected_project_id")
    if saved_id:
        for i, p in enumerate(projects):
            if p["id"] == saved_id:
                default_idx = i
                break

    project_name = st.selectbox("Projet", options=proj_names, index=default_idx, key="sel_project_name")
    project_id = next((p["id"] for p in projects if p["name"] == project_name), projects[0]["id"])
    st.session_state["selected_project_id"] = project_id

    # ⚠️ Ne pas utiliser enter_to_submit (non supporté dans ta version)
    with st.form("update_form", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            val_travaux = st.number_input("Progression travaux (%)", min_value=0.0, max_value=100.0, step=1.0, value=0.0)
        with col2:
            val_paiements = st.number_input("Progression paiements (%)", min_value=0.0, max_value=100.0, step=1.0, value=0.0)

        date_pv: Optional[date] = st.date_input("Date du PV de chantier (optionnel)", value=None, format="YYYY/MM/DD")
        commentaires = st.text_area("Commentaires", placeholder="Observations, risques, points bloquants…", height=140)

        st.markdown("#### Joindre le PV (PDF/DOCX/DOC)")
        upload_files = st.file_uploader(
            "Drag and drop files here", type=["pdf", "doc", "docx"], accept_multiple_files=True, label_visibility="collapsed"
        )

        submitted = st.form_submit_button("Enregistrer la mise à jour", type="primary")

    if submitted:
        user = st.session_state.get("user")
        if not user:
            st.error("Tu dois être connecté pour enregistrer.")
            return
        user_id = getattr(user, "id", None)
        if not user_id:
            st.error("Identifiant utilisateur introuvable.")
            return

        nb_ok, _uploaded = upload_pv_files(sb, project_id, date_pv or date.today(), upload_files or [])
        payload = {
            "project_id": project_id,
            "updated_by": user_id,
            "progress_travaux": float(val_travaux),
            "progress_paiements": float(val_paiements),
            "commentaires": commentaires or "",
            "pv_chantier": date_pv.isoformat() if isinstance(date_pv, date) else None,
        }
        try:
            sb.table("project_updates").insert(payload).execute()
            st.success(f"Mise à jour enregistrée. Fichiers déposés : {nb_ok}")
        except Exception as e:
            st.error(f"Erreur enregistrement mise à jour : {e}")

    st.divider()
    render_pv_history(sb, project_id)

# ------------- MAIN -------------
def main():
    try:
        sb = get_supabase()
    except Exception as e:
        st.error(f"Supabase non initialisé : {e}")
        return

    # Diagnostic
    test_connectivity_panel()

    # Auth
    user = st.session_state.get("user")
    if not user:
        user = login_panel(sb)
        return
    else:
        header_user(sb, user)

    # Projets & Form
    projects = list_projects(sb)
    form_panel(sb, projects)

if __name__ == "__main__":
    main()
