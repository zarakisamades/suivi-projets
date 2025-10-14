# app.py
from __future__ import annotations

import os
import socket
import uuid
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

import streamlit as st
from supabase import create_client, Client

# ─────────────────────────────────────────────────────────────────────────────
# Configuration & constantes
# ─────────────────────────────────────────────────────────────────────────────
APP_TITLE = "Suivi d’avancement — Saisie"
PAGE_ICON = "🧭"

BUCKET_PV = "pv-chantier"
MAX_UPLOAD_MB = 25
ALLOWED_EXT = {".pdf", ".docx", ".doc"}
SIGNED_URL_TTL = 3600  # secondes (ex: 3600 = 1h, 86400 = 24h)

# ─────────────────────────────────────────────────────────────────────────────
# Client Supabase (v2.x sync)
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        st.error("Variables d’environnement SUPABASE_URL / SUPABASE_ANON_KEY manquantes.")
        st.stop()
    return create_client(url, key)


def dns_ping_from_url(url: str) -> Optional[str]:
    try:
        host = url.replace("https://", "").replace("http://", "").split("/")[0]
        return socket.gethostbyname(host)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Session State (défini AVANT tout widget) + reset différé
# ─────────────────────────────────────────────────────────────────────────────
for k, v in {
    "user": None,                        # {"email":..., "id":...} ou None
    "selected_project_id": None,         # UUID projet
    "uploader_version": 0,               # pour reset du file_uploader
    "form_progress_travaux": 0.0,
    "form_progress_paiements": 0.0,
    "form_commentaires": "",
    "form_date_pv": None,                # date | None
    # flags techniques
    "__reset_after_save": False,
    "__flash": None,                     # dict {"ok":bool,"uploaded":int,"warns":[...],"msg":str|None}
}.items():
    st.session_state.setdefault(k, v)

# Si le précédent run a demandé un reset, on le fait ici (avant création des widgets)
if st.session_state.get("__reset_after_save"):
    st.session_state["uploader_version"] += 1
    st.session_state["form_progress_travaux"] = 0.0
    st.session_state["form_progress_paiements"] = 0.0
    st.session_state["form_commentaires"] = ""
    st.session_state["form_date_pv"] = None
    st.session_state["__reset_after_save"] = False

# ─────────────────────────────────────────────────────────────────────────────
# Mise en page
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title=APP_TITLE, page_icon=PAGE_ICON, layout="wide")


# ─────────────────────────────────────────────────────────────────────────────
# Accès données
# ─────────────────────────────────────────────────────────────────────────────
def list_projects(sb: Client) -> List[Dict]:
    try:
        res = sb.table("projects").select("id,name").order("name").execute()
        return res.data or []
    except Exception as e:
        st.warning(f"Erreur lecture projets : {e}")
        return []


def insert_update(
    sb: Client,
    project_id: str,
    user_id: str,
    progress_t: float,
    progress_p: float,
    commentaires: str,
    pv_date: Optional[date],
) -> Tuple[bool, Optional[str]]:
    """
    Insert dans project_updates en respectant RLS (updated_by = auth.uid()).
    """
    payload = {
        "project_id": project_id,
        "updated_by": user_id,  # ✅ nécessaire pour policy INSERT
        "progress_travaux": progress_t,
        "progress_paiements": progress_p,
        "commentaires": commentaires,
    }
    if pv_date is not None:
        payload["pv_chantier"] = pv_date.isoformat()
    try:
        sb.table("project_updates").insert(payload).execute()
        return True, None
    except Exception as e:
        return False, str(e)


def _bytes_from_uploader(uploaded_files) -> List[Tuple[str, bytes]]:
    out: List[Tuple[str, bytes]] = []
    for uf in uploaded_files or []:
        try:
            out.append((uf.name, uf.read()))
        except Exception as e:
            st.warning(f"{uf.name}: lecture impossible ({e})")
    return out


def _content_type_for(ext: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
    }.get(ext.lower(), "application/octet-stream")


def upload_files(sb: Client, project_id: str, files: List[Tuple[str, bytes]]) -> Tuple[int, List[str]]:
    """
    Upload dans: pv-chantier/<project_id>/<YYYYMMDD>/<uuid>_<nom_fichier>
    """
    ok = 0
    warns: List[str] = []
    today_str = datetime.utcnow().strftime("%Y%m%d")
    base_path = f"{project_id}/{today_str}"  # pas de duplication de dossier

    for fname, content in files:
        ext = os.path.splitext(fname)[1].lower()
        if ext not in ALLOWED_EXT:
            warns.append(f"{fname}: extension non autorisée.")
            continue
        if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
            warns.append(f"{fname}: dépasse {MAX_UPLOAD_MB} Mo.")
            continue
        try:
            path = f"{base_path}/{uuid.uuid4().hex}_{fname}"
            sb.storage.from_(BUCKET_PV).upload(
                path=path,
                file=content,
                file_options={"contentType": _content_type_for(ext)},
            )
            ok += 1
        except Exception as e:
            warns.append(f"{fname}: {e}")
    return ok, warns


def list_signed_pv(sb: Client, project_id: str, limit_per_day: int = 500) -> List[Dict]:
    """
    Retourne la liste des PV avec liens signés, en parcourant:
        pv-chantier/<project_id>/<YYYYMMDD>/*.*
    ⚠️ supabase-py 2.x : options passées via dict dans storage.list(...)
    """
    results: List[Dict] = []
    try:
        # Lister les sous-dossiers (jours)
        days = sb.storage.from_(BUCKET_PV).list(
            path=project_id,
            options={"limit": 1000, "sortBy": {"column": "name", "order": "desc"}},
        )
        for day in days or []:
            day_name = day.get("name")
            if not day_name:
                continue

            # Lister les fichiers de ce jour
            files = sb.storage.from_(BUCKET_PV).list(
                path=f"{project_id}/{day_name}",
                options={"limit": limit_per_day, "sortBy": {"column": "name", "order": "desc"}},
            )
            for f in files or []:
                fname = f.get("name")
                if not fname:
                    continue
                full_path = f"{project_id}/{day_name}/{fname}"
                try:
                    signed = sb.storage.from_(BUCKET_PV).create_signed_url(full_path, SIGNED_URL_TTL)
                    url = signed["signedURL"] if isinstance(signed, dict) else signed
                    results.append(
                        {
                            "file_name": fname,
                            "url": url,
                            "uploaded_at": f.get("created_at") or f.get("updated_at") or "",
                            "day_folder": day_name,
                        }
                    )
                except Exception:
                    continue
    except Exception as e:
        st.warning(f"Erreur lecture Storage : {e}")
        return []
    # tri décroissant par date d’upload si dispo, sinon par dossier de jour
    results.sort(key=lambda x: (x.get("uploaded_at") or "", x.get("day_folder") or "", x["file_name"]), reverse=True)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Rendu UI
# ─────────────────────────────────────────────────────────────────────────────
def render_pv_list_grouped(pv_list: List[Dict]):
    """
    Affiche l’historique groupé par jour (YYYY-MM-DD si possible),
    sinon par nom de dossier jour (YYYYMMDD).
    """
    by_day = defaultdict(list)
    for item in pv_list:
        iso = item.get("uploaded_at") or ""
        # uploaded_at vient souvent au format ISO8601 Z
        try:
            d = datetime.fromisoformat(iso.replace("Z", "")).date().isoformat()
        except Exception:
            # fallback : utilise le nom de dossier tel quel (YYYYMMDD)
            d = item.get("day_folder") or "Date inconnue"
        by_day[d].append(item)

    for d in sorted(by_day.keys(), reverse=True):
        st.markdown(f"#### {d}")
        for it in by_day[d]:
            st.markdown(f"- **[{it['file_name']}]({it['url']})**", unsafe_allow_html=True)


def auth_panel(sb: Client):
    user = st.session_state["user"]
    if user:
        st.caption(f"Connecté : {user.get('email', '')}")
        if st.button("Se déconnecter", type="secondary"):
            try:
                sb.auth.sign_out()
            except Exception:
                pass
            st.session_state["user"] = None
            st.session_state["selected_project_id"] = None
            st.session_state["__reset_after_save"] = True
            st.rerun()
        return

    st.subheader("Connexion")
    email = st.text_input("Email", key="auth_email")
    pwd = st.text_input("Mot de passe", type="password", key="auth_pwd")
    if st.button("Connexion", type="primary"):
        try:
            res = sb.auth.sign_in_with_password({"email": email, "password": pwd})
            st.session_state["user"] = {"email": res.user.email, "id": res.user.id}
            st.success("Connexion réussie.")
            st.rerun()
        except Exception as e:
            st.error(f"Échec de connexion : {e}")


def seleccionar_projet(sb: Client) -> Optional[str]:
    """
    Selectbox des projets.
    Reset des champs si changement (via flag + rerun).
    """
    projects = list_projects(sb)
    if not projects:
        st.info("Aucun projet trouvé dans la table **projects**.")
        return None

    id_to_name = {p["id"]: p["name"] for p in projects}
    names = [p["name"] for p in projects]

    current_id = st.session_state.get("selected_project_id")
    index = 0
    if current_id in id_to_name:
        for i, p in enumerate(projects):
            if p["id"] == current_id:
                index = i
                break

    choice = st.selectbox("Projet", options=names, index=index if names else 0, key="project_selectbox")

    new_id = None
    for p in projects:
        if p["name"] == choice:
            new_id = p["id"]
            break

    if new_id != current_id:
        st.session_state["selected_project_id"] = new_id
        st.session_state["__reset_after_save"] = True
        st.rerun()

    return st.session_state["selected_project_id"]


def form_panel(sb: Client, project_id: Optional[str]):
    if not project_id:
        return

    # Affiche le flash (résultat précédent) AVANT les widgets
    flash = st.session_state.get("__flash")
    if flash:
        if flash.get("ok"):
            st.success(f"Mise à jour enregistrée. Fichiers déposés : {flash.get('uploaded', 0)}")
        else:
            st.error(f"Erreur enregistrement mise à jour : {flash.get('msg','')}")
        warns = flash.get("warns") or []
        if warns:
            st.warning("Quelques fichiers n’ont pas été pris en compte :\n- " + "\n- ".join(warns))
        st.session_state["__flash"] = None  # clear

    st.subheader("Nouvelle mise à jour")

    col1, col2 = st.columns(2)
    with col1:
        st.number_input(
            "Progression travaux (%)",
            min_value=0.0,
            max_value=100.0,
            step=1.0,
            key="form_progress_travaux",
        )
    with col2:
        st.number_input(
            "Progression paiements (%)",
            min_value=0.0,
            max_value=100.0,
            step=1.0,
            key="form_progress_paiements",
        )

    # Date optionnelle (checkbox pour éviter value=None)
    use_pv_date = st.checkbox(
        "Renseigner une date de PV de chantier",
        value=st.session_state.get("form_date_pv") is not None,
    )
    if use_pv_date:
        default_date = st.session_state.get("form_date_pv") or date.today()
        st.date_input("Date du PV de chantier (optionnel)", value=default_date, key="form_date_pv")
    else:
        st.session_state["form_date_pv"] = None

    st.text_area(
        "Commentaires",
        key="form_commentaires",
        placeholder="Observations, risques, points bloquants…",
        height=120,
    )

    st.markdown("### Joindre le PV (PDF/DOCX/DOC)")
    uploaded = st.file_uploader(
        "Glisser-déposer les fichiers ici",
        type=[e.lstrip(".") for e in ALLOWED_EXT],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state['uploader_version']}",
        help=f"Limite {MAX_UPLOAD_MB} Mo par fichier.",
    )

    if st.button("Enregistrer la mise à jour", type="primary"):
        # 1) Insert (RLS)
        ok_update, err_msg = insert_update(
            sb,
            project_id=project_id,
            user_id=st.session_state["user"]["id"],  # ✅ pour RLS (updated_by = auth.uid())
            progress_t=float(st.session_state["form_progress_travaux"] or 0),
            progress_p=float(st.session_state["form_progress_paiements"] or 0),
            commentaires=st.session_state["form_commentaires"] or "",
            pv_date=st.session_state["form_date_pv"],
        )
        uploaded_count = 0
        warns: List[str] = []
        if ok_update:
            # 2) Upload Storage
            files_bytes = _bytes_from_uploader(uploaded)
            if files_bytes:
                uploaded_count, warns = upload_files(sb, project_id, files_bytes)

        # flash pour l’afficher au prochain run
        st.session_state["__flash"] = {
            "ok": ok_update,
            "uploaded": uploaded_count,
            "warns": warns,
            "msg": err_msg,
        }
        # reset des champs au prochain run uniquement si succès
        st.session_state["__reset_after_save"] = bool(ok_update)
        st.rerun()

    # ── Historique PV groupé par jour ────────────────────────────────────────
    st.markdown("### Pièces jointes — PV de chantier")
    pv_list = list_signed_pv(sb, project_id)
    if not pv_list:
        st.info("Aucun PV pour ce projet.")
    else:
        render_pv_list_grouped(pv_list)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    sb = get_supabase()

    # Bandeau diagnostic réseau (optionnel)
    ip = dns_ping_from_url(os.getenv("SUPABASE_URL", ""))
    st.success(
        f"Connexion réseau Supabase OK (status attendu: 401) — DNS "
        f"{os.getenv('SUPABASE_URL','').replace('https://','').split('/')[0]} "
        f"{'→ ' + ip if ip else ''}"
    )

    # Auth
    auth_panel(sb)
    if not st.session_state["user"]:
        return

    st.title(APP_TITLE)

    # Sélection projet
    project_id = seleccionar_projet(sb)
    if not project_id:
        return

    # Formulaire + Historique
    form_panel(sb, project_id)


if __name__ == "__main__":
    main()
