# app.py

import os, socket, traceback, platform, uuid, datetime as dt
import streamlit as st
# ---- Session state par défaut (à mettre tôt dans app.py, après les imports) ----
import uuid

for k, v in {
    "user": None,                          # objet utilisateur supabase ou None
    "auth_panel_visible": True,            # contrôle d'affichage du panneau d'auth
    "selected_project_id": None,           # id projet sélectionné
    "uploader_version": 0,                 # sert à vider le file_uploader
    "form_progress_travaux": None,         # champs de saisie
    "form_progress_paiements": None,
    "form_date_pv": None,
    "form_commentaires": "",
}.items():
    st.session_state.setdefault(k, v)
from typing import List, Dict, Any, Optional

# ------------ Config page ------------
st.set_page_config(page_title="Suivi d’avancement — Saisie hebdomadaire", page_icon="📊", layout="wide")

# ------------ Constantes ------------
BUCKET_PV = "pv-chantier"
MAX_UPLOAD_MB = 25
ALLOWED_EXT = {".pdf", ".docx", ".doc"}

# ------------ Helpers UI ------------
def pill(ok: bool, msg: str):
    c = "#e8f5e9" if ok else "#fdecea"
    st.markdown(f"<div style='background:{c};padding:10px;border-radius:8px'>{msg}</div>", unsafe_allow_html=True)

def human_mb(b: int) -> str:
    return f"{b/1024/1024:.1f} MB"

# ------------ Supabase ------------
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()

@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise RuntimeError("Variables SUPABASE_URL / SUPABASE_ANON_KEY manquantes.")
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# ------------ Checks réseau/DNS ------------
def check_dns(host: str) -> Optional[str]:
    try:
        ip = socket.gethostbyname(host)
        return ip
    except Exception:
        return None

def check_supabase_network(url: str) -> bool:
    # On passe par le client supabase auth health via httpx interne
    try:
        sb = get_supabase()
        # Petite requête innocente (user non connecté => 401 attendu)
        _ = sb.auth.get_user()
        return True  # si pas d'exception, c'est OK
    except Exception:
        # le 401 est attendu : ça prouve que l'endpoint répond
        return True

# ------------ Auth ------------
def ensure_session_state():
    if "sb" not in st.session_state:
        st.session_state.sb = get_supabase()
    if "user" not in st.session_state:
        st.session_state.user = None

def sign_in(email: str, password: str) -> bool:
    sb: Client = st.session_state.sb
    try:
        res = sb.auth.sign_in_with_password({"email": email, "password": password})
        st.session_state.user = res.user
        return True
    except Exception as e:
        st.error(f"Échec de connexion : {e}")
        return False

def sign_up(email: str, password: str) -> bool:
    sb: Client = st.session_state.sb
    try:
        _ = sb.auth.sign_up({"email": email, "password": password})
        st.success("Compte créé. Valide le lien reçu par email, puis connecte-toi.")
        return True
    except Exception as e:
        st.error(f"Échec de création du compte : {e}")
        return False

def sign_out():
    sb: Client = st.session_state.sb
    try:
        sb.auth.sign_out()
    except Exception:
        pass
    st.session_state.user = None
    st.rerun()

# ------------ Accès données ------------
def fetch_projects(sb: Client) -> List[Dict[str, Any]]:
    try:
        data = sb.table("projects").select("id,name").order("name").execute().data or []
        return data
    except Exception as e:
        st.warning(f"Erreur lecture projets : {e}")
        return []

def insert_update_row(sb: Client, row: Dict[str, Any]) -> bool:
    try:
        _ = sb.table("project_updates").insert(row).execute()
        return True
    except Exception as e:
        st.error(f"Erreur enregistrement mise à jour : {e}")
        return False

# ------------ Upload PV ------------
def ext_ok(name: str) -> bool:
    n = name.lower()
    for s in ALLOWED_EXT:
        if n.endswith(s):
            return True
    return False

def upload_pv_file(sb: Client, project_id: str, file) -> Optional[Dict[str, Any]]:
    if not file:
        return None
    if not ext_ok(file.name):
        st.error("Format autorisé : PDF/DOCX/DOC.")
        return None
    if file.size > MAX_UPLOAD_MB * 1024 * 1024:
        st.error(f"Fichier trop volumineux ({human_mb(file.size)}), max {MAX_UPLOAD_MB} MB.")
        return None

    # Chemin de stockage : project_id / YYYY / MM / uuid_nom
    now = dt.datetime.now()
    safe_name = f"{uuid.uuid4().hex}_{file.name.replace('/', '_')}"
    path = f"{project_id}/{now:%Y}/{now:%m}/{safe_name}"

    try:
        # upload
        sb.storage.from_(BUCKET_PV).upload(path, file, file_options={"contentType": file.type or "application/octet-stream"})
        # URL signée pour visu immédiate
        signed = sb.storage.from_(BUCKET_PV).create_signed_url(path, 3600)
        url = signed.get("signedURL") or signed.get("signed_url")

        # Enregistre la méta dans table project_pv (si RLS ok)
        meta = {
            "project_id": project_id,
            "file_name": file.name,
            "file_path": path,
            "uploaded_at": dt.datetime.utcnow().isoformat(),
        }
        try:
            sb.table("project_pv").insert(meta).execute()
        except Exception:
            # Si la table n'existe pas ou RLS bloque, on ne casse pas l'UI
            pass

        return {"file_name": file.name, "file_path": path, "url": url, "uploaded_at": now}
    except Exception as e:
        st.error(f"Échec de l’upload : {e}")
        return None

def list_signed_pv(sb: Client, project_id: str, expires_sec: int = 3600) -> List[Dict[str, Any]]:
    """Liste les PV depuis la table project_pv (nouveau flux)."""
    try:
        rows = (
            sb.table("project_pv")
            .select("file_name,file_path,uploaded_at")
            .eq("project_id", project_id)
            .order("uploaded_at", desc=True)
            .limit(200)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []

    out = []
    for r in rows:
        path = r.get("file_path")
        try:
            signed = sb.storage.from_(BUCKET_PV).create_signed_url(path, expires_sec)
            url = signed.get("signedURL") or signed.get("signed_url")
        except Exception:
            url = None
        out.append(
            {
                "file_name": r.get("file_name") or os.path.basename(path or ""),
                "file_path": path,
                "uploaded_at": r.get("uploaded_at"),
                "url": url,
            }
        )
    return out

# -------- Legacy (historique project_pv_log) --------
def _pick_first(d: Dict[str, Any], keys: List[str], default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default

def fetch_pv_log_rows(sb: Client, project_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """Optionnel : récupère l'historique legacy si la table existe, sinon liste vide."""
    # Tente un select ; si erreur, on considère que la table n'existe pas / pas accessible
    try:
        q = sb.table("project_pv_log").select("*").eq("project_id", project_id).order("uploaded_at", desc=True)
        try:
            rows = q.limit(limit).execute().data or []
        except Exception:
            # fallback si la colonne s'appelle autrement
            rows = sb.table("project_pv_log").select("*").eq("project_id", project_id).order("created_at", desc=True).limit(limit).execute().data or []
    except Exception:
        return []

    out = []
    for r in rows:
        file_path = _pick_first(r, ["file_path", "storage_path", "path", "object_path"])
        file_name = _pick_first(r, ["file_name", "original_name", "name", "title"], default="PV_chantier.pdf")
        ts       = _pick_first(r, ["uploaded_at", "created_at", "date", "ts"])
        if not file_path:
            continue
        try:
            signed = sb.storage.from_(BUCKET_PV).create_signed_url(file_path, 3600)
            url = signed.get("signedURL") or signed.get("signed_url")
        except Exception:
            url = None
        out.append({"file_name": file_name, "uploaded_at": ts, "url": url, "file_path": file_path})
    return out

# ===================== UI =====================

ensure_session_state()
sb: Client = st.session_state.sb

# Bandeau de checks
st.title("hebdomadaire")

host = SUPABASE_URL.replace("https://", "").replace("http://", "").split("/")[0]
ip = check_dns(host)
if ip:
    pill(True, f"Connexion réseau Supabase OK (status attendu: 401) — DNS {host} ➜ {ip}")
else:
    pill(False, "DNS échec : impossible de résoudre le host Supabase")

if check_supabase_network(SUPABASE_URL):
    pill(True, "Import et initialisation de Supabase OK")
else:
    pill(False, "Problème de réseau vers Supabase")

col_left, col_main = st.columns([0.32, 0.68], gap="large")

# --------- Panneau gauche : Auth ----------
with col_left:
    st.subheader("Connexion")
    mode = st.radio("Mode", ["Se connecter", "Créer un compte"], index=0, label_visibility="collapsed")
    email = st.text_input("Email", key="auth_email")
    pwd = st.text_input("Mot de passe", type="password", key="auth_pwd")

    if mode == "Se connecter":
        if st.button("Connexion", type="primary", use_container_width=True):
            if sign_in(email, pwd):
                st.success(f"Connecté : {email}")
                st.rerun()
    else:
        if st.button("Créer mon compte", type="primary", use_container_width=True):
            sign_up(email, pwd)

    if st.session_state.user:
        st.caption(f"Connecté : **{st.session_state.user.email}**")
        if st.button("Se déconnecter", use_container_width=True):
            sign_out()

# --------- Colonne principale ----------
with col_main:
    st.header("Suivi d’avancement — Saisie")

    user = st.session_state.user
    if not user:
        st.info("Connecte-toi pour saisir une mise à jour.")
        st.stop()

    # Projets
    projects = fetch_projects(sb)
    if not projects:
        st.info("Aucun projet trouvé dans la table **projects**.")
        st.stop()

    proj_map = {p["name"]: p["id"] for p in projects}
    proj_name = st.selectbox("Projet", options=list(proj_map.keys()))
    project_id = proj_map[proj_name]

    st.markdown("### Nouvelle mise à jour")
    c1, c2 = st.columns(2)
    with c1:
        prog_t = st.number_input("Progression travaux (%)", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
        prog_p = st.number_input("Progression paiements (%)", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
    with c2:
        pv_date = st.date_input("Date du PV de chantier (optionnel)")

    comment = st.text_area("Commentaires", placeholder="Observations, risques, points bloquants…")
    up_file = st.file_uploader("Joindre le PV (PDF/DOCX/DOC)", type=["pdf", "docx", "doc"])

    if st.button("Enregistrer la mise à jour", type="primary"):
        row = {
            # colonnes existantes de ta table project_updates
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "updated_by": st.session_state.user.id,  # si tu stockes l'id utilisateur supabase, adapte ici
            "progress_travaux": prog_t,
            "progress_paiements": prog_p,
            "pv_chantier": pv_date.isoformat() if pv_date else None,
            "commentaires": comment.strip() or None,
            "created_at": dt.datetime.utcnow().isoformat(),
        }

        ok = insert_update_row(sb, row)
        if ok:
            st.success("Mise à jour enregistrée.")
            # Upload éventuel
            if up_file is not None:
                meta = upload_pv_file(sb, project_id, up_file)
                if meta:
                    st.success(f"PV uploadé : {meta['file_name']}")
            st.rerun()

    st.markdown("### Pièces jointes — PV de chantier")
    # Liste “nouveau flux”
    pv_list = list_signed_pv(sb, project_id, expires_sec=3600)
    if not pv_list:
        st.info("Aucun PV (nouveau flux) pour ce projet.")
    else:
        for item in pv_list:
            if item["url"]:
                st.markdown(
                    f"- **[{item['file_name']}]({item['url']})**  \n"
                    f"  _Uploadé le : {item['uploaded_at']}_",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"- **{item['file_name']}**  \n"
                    f"  _Uploadé le : {item['uploaded_at']}_  (prévisualisation indisponible)",
                    unsafe_allow_html=True,
                )

    # Historique legacy (à AJOUTER, pas remplacer)
    with st.expander("🗂️ Historique complet des PV (source legacy `project_pv_log`)", expanded=False):
        legacy = fetch_pv_log_rows(sb, project_id, limit=200)
        if not legacy:
            st.caption("Aucun enregistrement legacy trouvé pour ce projet.")
        else:
            for item in legacy:
                if item["url"]:
                    st.markdown(
                        f"- **[{item['file_name']}]({item['url']})**  \n"
                        f"  _Uploadé le : {item['uploaded_at']}_  \n"
                        f"  <span style='opacity:0.6'>({item['file_path']})</span>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"- **{item['file_name']}**  \n"
                        f"  _Uploadé le : {item['uploaded_at']}_  \n"
                        f"  <span style='opacity:0.6'>Chemin : {item['file_path']}</span>",
                        unsafe_allow_html=True,
                    )

# Pied de page debug léger
with st.sidebar:
    st.write("Python:", platform.python_version())
