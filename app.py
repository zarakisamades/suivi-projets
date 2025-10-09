# =========================
# PV DE CHANTIER (upload + listing)
# A placer dans la page "Nouvelle mise à jour"
# =========================
from datetime import datetime
import re
import mimetypes
import streamlit as st

BUCKET_PV = "pv-chantier"

def _safe_filename(name: str) -> str:
    # Nettoyer un peu
    base = re.sub(r"\s+", "_", name.strip())
    base = re.sub(r"[^A-Za-z0-9._-]", "", base)
    return base or f"pv_{int(datetime.utcnow().timestamp())}.pdf"

def upload_pv_file(sb, project_id, file_obj):
    """Upload 1 fichier dans Storage + insert metadata dans public.project_pv"""
    # Chemin = {project_id}/{YYYY}/{YYYY-MM-DD_HH-mm-ss}_{nom}
    now = datetime.utcnow()
    ts = now.strftime("%Y-%m-%d_%H-%M-%S")
    fname = _safe_filename(file_obj.name)
    path = f"{project_id}/{now.year}/{ts}_{fname}"

    # Déterminer le MIME
    mime = getattr(file_obj, "type", None) or mimetypes.guess_type(fname)[0] or "application/octet-stream"

    # Upload (no upsert)
    up = sb.storage.from_(BUCKET_PV).upload(
        path=path,
        file=file_obj.getvalue(),  # Streamlit UploadedFile
        file_options={"content-type": mime, "upsert": False},
    )
    if isinstance(up, dict) and up.get("error"):
        raise RuntimeError(f"Erreur upload: {up['error'].get('message', up['error'])}")

    # Insert metadata
    ins = sb.table("project_pv").insert({
        "project_id": project_id,
        "file_path": path,
        "file_name": file_obj.name,
        "mime_type": mime,
    }).execute()
    if ins.error:
        raise RuntimeError(f"Erreur DB: {ins.error.message}")

def list_signed_pv(sb, project_id, expires_sec=3600):
    """Retourne la liste des PV (dict) avec URL signée temporaire."""
    sel = sb.table("project_pv")\
            .select("*")\
            .eq("project_id", project_id)\
            .order("uploaded_at", desc=True)\
            .execute()
    if sel.error:
        st.error(f"Erreur lecture PV: {sel.error.message}")
        return []

    out = []
    for r in sel.data:
        signed = sb.storage.from_(BUCKET_PV).create_signed_url(r["file_path"], expires_sec)
        url = signed.get("signedURL") or signed.get("signed_url")
        out.append({
            "file_name": r["file_name"],
            "uploaded_at": r["uploaded_at"],
            "url": url,
        })
    return out


# -------------------------
# UI : section PV de chantier
# -------------------------
st.subheader("📎 PV de chantier (PDF / DOCX)")

# Si tu as déjà le project_id sélectionné plus haut, remplace cette partie :
# ------------------------------------------------------------------------
# Sélection (fallback) si tu n'as PAS déjà la variable project_id
projects_q = sb.table("projects").select("id,name").order("name").execute()
projects = projects_q.data or []
project_labels = {p["name"]: p["id"] for p in projects}
if not projects:
    st.info("Aucun projet trouvé. Ajoute un projet avant d’uploader des PV.")
    selected_project_id = None
else:
    selected_name = st.selectbox("Projet", list(project_labels.keys()), index=0, key="pv_select_project")
    selected_project_id = project_labels[selected_name]
# ------------------------------------------------------------------------
# Si tu as déjà `project_id` dans ta page, fais simplement :
# selected_project_id = project_id

# Uploader
col1, col2 = st.columns([2,1])
with col1:
    files = st.file_uploader(
        "Dépose un ou plusieurs fichiers",
        type=["pdf", "doc", "docx"],
        accept_multiple_files=True
    )
with col2:
    st.write("")  # espace
    can_upload = st.button("Uploader les PV", use_container_width=True, disabled=not (files and selected_project_id))

if can_upload and selected_project_id:
    ok, ko = 0, 0
    with st.spinner("Upload en cours..."):
        for f in files:
            try:
                upload_pv_file(sb, selected_project_id, f)
                ok += 1
            except Exception as e:
                ko += 1
                st.error(str(e))
    if ok:
        st.success(f"{ok} fichier(s) uploadé(s) ✔️")
    if ko:
        st.warning(f"{ko} fichier(s) en erreur ⚠️")
    st.rerun()

# Liste des PV déjà uploadés (liens signés)
st.markdown("#### PV déjà uploadés")
if selected_project_id:
    pv_list = list_signed_pv(sb, selected_project_id, expires_sec=3600)
    if not pv_list:
        st.info("Aucun PV trouvé pour ce projet.")
    else:
        for item in pv_list:
            # Affichage : Nom cliquable + date
            st.markdown(
                f"- **[{item['file_name']}]({item['url']})**  \n"
                f"  _Uploadé le : {item['uploaded_at']}_",
                unsafe_allow_html=True
            )
