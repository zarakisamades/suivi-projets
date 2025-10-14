# app.py — SAFE-MIN (auth + upload PV + journal pv_files + historique)
import os, socket, uuid, mimetypes, traceback
from datetime import date, datetime
from typing import List, Dict, Optional, Tuple

import streamlit as st
from supabase import create_client, Client

# ───────────────────────── Config page ─────────────────────────
st.set_page_config(page_title="Suivi — PV de chantier", page_icon="🗂️", layout="wide")

# ───────────────────────── Constantes ─────────────────────────
BUCKET_PV = "pv-chantier"     # bucket PUBLIC (lecture)
MAX_UPLOAD_MB = 200
ALLOWED_EXT = {".pdf", ".docx", ".doc"}

# ───────────────────────── Utilitaires ─────────────────────────
def env(name: str) -> str:
    return (os.getenv(name) or "").strip()

@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    url, key = env("SUPABASE_URL"), env("SUPABASE_ANON_KEY")
    if not url or not key:
        st.error("Variables d’env. SUPABASE_URL / SUPABASE_ANON_KEY manquantes.")
        st.stop()
    return create_client(url, key)

def dns_banner():
    url = env("SUPABASE_URL")
    try:
        host = url.split("//",1)[1].split("/",1)[0]
        ip = socket.gethostbyname(host)
        st.caption(f"DNS OK → {host} : {ip}")
    except Exception as e:
        st.warning(f"DNS KO : {e}")

def safe_name(original: str) -> str:
    base = os.path.basename(original or "").replace(" ", "_")
    base = "".join(c for c in base if c.isalnum() or c in ("_", "-", ".", "(", ")")) or "document"
    return f"{uuid.uuid4().hex}_{base}"

def content_type(fname: str) -> str:
    ct, _ = mimetypes.guess_type(fname)
    return ct or "application/octet-stream"

# ───────────────────────── Auth ─────────────────────────
def login_panel(sb: Client):
    st.subheader("Connexion")
    mode = st.radio(" ", ["Se connecter", "Créer un compte"], label_visibility="collapsed", key="auth_mode")
    email = st.text_input("Email", key="auth_email")
    pwd   = st.text_input("Mot de passe", type="password", key="auth_pwd")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Connexion") and mode == "Se connecter":
            try:
                res = sb.auth.sign_in_with_password({"email": email, "password": pwd})
                st.session_state["user"] = res.user
                st.success("Connecté.")
                st.rerun()
            except Exception as e:
                st.error(f"Echec connexion : {e}")
    with c2:
        if st.button("Créer le compte") and mode == "Créer un compte":
            try:
                sb.auth.sign_up({"email": email, "password": pwd})
                st.info("Compte créé. Vérifie ton email puis connecte-toi.")
            except Exception as e:
                st.error(f"Echec création compte : {e}")

def header_user(sb: Client, user):
    st.caption(f"Connecté : {user.get('email')}")
    if st.button("Se déconnecter"):
        try: sb.auth.sign_out()
        except Exception: pass
        st.session_state.pop("user", None)
        st.rerun()

# ───────────────────────── Données ─────────────────────────
def list_projects(sb: Client) -> List[Dict]:
    try:
        res = sb.table("projects").select("id, name").order("name").execute()
        return res.data or []
    except Exception as e:
        st.error(f"Lecture projets : {e}")
        return []

def get_public_url(sb: Client, obj_path: str) -> str:
    pub = sb.storage.from_(BUCKET_PV).get_public_url(obj_path)
    return pub.get("publicURL") or pub.get("public_url") or ""

def log_pv_file(sb: Client, project_id: str, file_name: str, obj_path: str,
                public_url: str, pv_date: Optional[date], uploaded_by: Optional[str]) -> None:
    row = {
        "project_id": project_id,
        "file_path": obj_path,
        "file_name": file_name,
        "public_url": public_url,
        "pv_date": pv_date.isoformat() if pv_date else None,
        "uploaded_by": uploaded_by,
    }
    sb.table("pv_files").insert(row).execute()

def upload_pv_files(sb: Client, project_id: str, files, pv_date: Optional[date], uploaded_by: Optional[str]) -> Tuple[int, List[str]]:
    msgs: List[str] = []
    if not files: return 0, msgs
    store = sb.storage.from_(BUCKET_PV)
    folder = (pv_date or date.today()).strftime("%Y%m%d")
    ok = 0
    for f in files:
        ext = os.path.splitext(f.name)[1].lower()
        if ext not in ALLOWED_EXT:
            msgs.append(f"⛔ {f.name} : extension interdite"); continue
        if f.size and f.size > MAX_UPLOAD_MB * 1024 * 1024:
            msgs.append(f"⛔ {f.name} : > {MAX_UPLOAD_MB}MB"); continue

        sname = safe_name(f.name)
        path = f"{project_id}/{folder}/{sname}"
        try:
            store.upload(path, f.read(), file_options={"content-type": content_type(f.name), "upsert": False})
            url = get_public_url(sb, path)
            try:
                log_pv_file(sb, project_id, sname, path, url, pv_date, uploaded_by)
            except Exception as e:
                msgs.append(f"⚠️ Journalisation pv_files échouée : {e}")
            msgs.append(f"✅ {f.name} déposé")
            ok += 1
        except Exception as e:
            msgs.append(f"⚠️ Upload échoué {f.name} : {e}")
    return ok, msgs

def fetch_pv_rows(sb: Client, project_id: str) -> List[Dict]:
    try:
        res = (sb.table("pv_files")
               .select("file_name, public_url, uploaded_at, pv_date")
               .eq("project_id", project_id)
               .order("uploaded_at", desc=True)
               .limit(500)
               .execute())
        return res.data or []
    except Exception as e:
        st.error(f"Lecture pv_files : {e}")
        return []

# ───────────────────────── UI Historique ─────────────────────────
def render_history(sb: Client, project_id: Optional[str]):
    st.subheader("Pièces jointes — PV de chantier")
    if not project_id:
        st.info("Sélectionne un projet pour voir les PV."); return
    rows = fetch_pv_rows(sb, project_id)
    if not rows:
        st.info("Aucun PV pour ce projet."); return

    grouped: Dict[str, List[Dict]] = {}
    for r in rows:
        d = r.get("pv_date")
        if not d:
            up = (r.get("uploaded_at") or "")[:10]
            d = up or date.today().isoformat()
        grouped.setdefault(d, []).append(r)

    for d in sorted(grouped.keys(), reverse=True):
        st.markdown(f"### {d}")
        for r in grouped[d]:
            st.markdown(f"- [{r['file_name']}]({r['public_url']}) — ajouté le `{r['uploaded_at']}`")

# ───────────────────────── UI Formulaire ─────────────────────────
def form_panel(sb: Client, projects: List[Dict]):
    st.header("Suivi — Nouvelle mise à jour (PV)")
    if not projects:
        st.info("Aucun projet disponible."); return

    names = [p["name"] for p in projects]
    by_name = {p["name"]: p["id"] for p in projects}
    sel_name = st.selectbox("Projet", names, key="sel_project")
    project_id = by_name.get(sel_name)

    pv_date = st.date_input("Date du PV (optionnel)", value=None, format="YYYY/MM/DD", key="pv_date")
    st.subheader("Joindre le PV (PDF/DOCX/DOC)")
    files = st.file_uploader(" ", type=["pdf", "docx", "doc"], accept_multiple_files=True, label_visibility="collapsed", key="pv_files")
    if st.button("Déposer les fichiers", type="primary"):
        user = st.session_state.get("user") or {}
        uploaded_by = user.get("id") or user.get("user_metadata", {}).get("sub")
        ok, msgs = upload_pv_files(sb, project_id, files, pv_date, uploaded_by)
        if msgs:
            with st.expander("Détails"):
                for m in msgs: st.write(m)
        if ok > 0: st.success(f"{ok} fichier(s) déposé(s)."); st.rerun()
        else: st.info("Aucun fichier déposé.")

    st.markdown("---")
    render_history(sb, project_id)

# ───────────────────────── Main ─────────────────────────
def main():
    dns_banner()
    sb = get_supabase()

    left, right = st.columns([1, 2], gap="large")
    with left:
        user = st.session_state.get("user")
        if not user:
            login_panel(sb)
        else:
            header_user(sb, user)

    with right:
        user = st.session_state.get("user")
        if not user:
            st.info("Connecte-toi pour continuer."); return
        projects = list_projects(sb)
        form_panel(sb, projects)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        st.error("Une erreur est survenue.")
        st.code(traceback.format_exc())
