# app.py

from __future__ import annotations
import os
import re
import io
import socket
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import streamlit as st
from supabase import Client, create_client


# =========================
# 0) ÉTAT GLOBAL / CONSTANTES
# =========================

DEFAULT_STATE = {
    "user": None,                     # objet user supabase (ou None)
    "auth_panel_visible": True,       # afficher/masquer le panneau d'auth
    "selected_project_id": None,      # id du projet sélectionné
    "last_project_id": None,          # pour détecter le changement de projet
    "form_progress_travaux": None,
    "form_progress_paiements": None,
    "form_date_pv": None,             # date du PV (optionnelle)
    "form_commentaires": "",
    "uploader_version": 0,            # permet de vider le file_uploader
}
for k, v in DEFAULT_STATE.items():
    if k not in st.session_state:
        st.session_state[k] = v

# Configuration page Streamlit
st.set_page_config(
    page_title="Suivi d’avancement — Saisie hebdomadaire",
    page_icon="📊",
    layout="wide",
)

# Constantes appli
BUCKET_PV = "pv-chantier"
MAX_UPLOAD_MB = 25
ALLOWED_EXT = {".pdf", ".docx", ".doc"}
SIGNED_URL_EXPIRES = 60 * 10  # 10 min


# =========================
# 1) SUPABASE
# =========================

@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        st.stop()  # Secrets manquants

    # Vérif DNS (affichage discret)
    try:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        _ = socket.gethostbyname(host)
        st.caption(f"Connexion réseau Supabase OK (status attendu: 401) — DNS {host}")
    except Exception as e:
        st.error(f"Erreur DNS vers Supabase : {e}")

    return create_client(url, key)


sb = get_supabase()


# =========================
# 2) OUTILS / HELPERS
# =========================

def _mime_for(name: str) -> str:
    ext = os.path.splitext(name.lower())[1]
    if ext == ".pdf":
        return "application/pdf"
    if ext == ".docx":
        # docx = zip + xml
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext == ".doc":
        return "application/msword"
    return "application/octet-stream"


def _safe_name(name: str) -> str:
    base = os.path.basename(name)
    # caractères sûrs
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip()
    # pas de doublons d'espaces
    base = re.sub(r"\s+", " ", base)
    return base or f"fichier_{int(datetime.now().timestamp())}"


def fetch_projects() -> List[Dict]:
    res = sb.table("projects").select("id,name").order("name").execute()
    return res.data or []


def insert_project_update(
    project_id: str,
    user_id: str,
    progress_travaux: Optional[float],
    progress_paiements: Optional[float],
    pv_date: Optional[date],
    commentaires: str,
) -> None:
    row = {
        "project_id": project_id,
        "updated_by": user_id,
        "progress_travaux": float(progress_travaux) if progress_travaux is not None else None,
        "progress_paiements": float(progress_paiements) if progress_paiements is not None else None,
        "pv_chantier": str(pv_date) if pv_date else None,
        "commentaires": (commentaires or "").strip(),
    }
    sb.table("project_updates").insert(row).execute()


def upload_pv_files(project_id: str, user_id: str, files: List) -> Tuple[int, List[str]]:
    """
    Dépose les fichiers dans Storage et inscrit dans project_pv_log.
    Retourne (nb_uploads, erreurs[])
    """
    if not files:
        return 0, []

    errs: List[str] = []
    ok = 0
    bucket = sb.storage.from_(BUCKET_PV)

    today = date.today().isoformat()
    for f in files:
        name = _safe_name(f.name)
        ext = os.path.splitext(name)[1].lower()
        size_mb = (len(f.getvalue()) / (1024 * 1024)) if hasattr(f, "getvalue") else 0

        if ext not in ALLOWED_EXT:
            errs.append(f"Extension non autorisée: {name}")
            continue
        if size_mb > MAX_UPLOAD_MB:
            errs.append(f"Fichier trop volumineux (> {MAX_UPLOAD_MB} Mo): {name}")
            continue

        object_key = f"{project_id}/{today}/{int(datetime.now().timestamp())}_{name}"
        data = f.read() if hasattr(f, "read") else f.getvalue()
        f.seek(0)  # au cas où

        try:
            bucket.upload(
                path=object_key,
                file=io.BytesIO(data),
                file_options={"content-type": _mime_for(name)},
            )
            # indexation
            sb.table("project_pv_log").insert({
                "project_id": project_id,
                "file_name": name,
                "object_key": object_key,
                "uploaded_by": user_id,
            }).execute()
            ok += 1
        except Exception as e:
            errs.append(f"{name}: {e}")

    return ok, errs


def list_signed_pv(project_id: str, expires_sec: int = SIGNED_URL_EXPIRES) -> List[Dict]:
    """Liste les PV d’un projet (liens signés récents)"""
    bucket = sb.storage.from_(BUCKET_PV)
    # on liste récursivement
    files = bucket.list(path=f"{project_id}")
    items: List[Dict] = []

    # Storage Python v2 liste plat : on relance list() par sous-dossier si besoin
    stack = [("", files)]
    while stack:
        prefix, arr = stack.pop()
        for it in arr:
            name = it.get("name") or it.get("Key") or ""
            if not name:
                continue
            # Dossier ?
            if it.get("id") is None and it.get("updated_at") is None and it.get("created_at") is None and name and name.endswith("/"):
                # Essayer de lister ce sous-dossier
                sub = bucket.list(path=f"{project_id}/{prefix}{name}".strip("/"))
                stack.append((f"{prefix}{name}", sub))
                continue

            object_key = f"{project_id}/{prefix}{name}".strip("/")
            try:
                signed = bucket.create_signed_url(object_key, expires_sec)
                url = signed.get("signedURL") or signed.get("signed_url")
                items.append({
                    "file_name": name,
                    "object_key": object_key,
                    "url": url,
                    "uploaded_at": it.get("updated_at") or it.get("created_at") or "",
                })
            except Exception:
                # on ignore les ratés de signature
                pass

    # tri inverse par date de chemin (approx) + nom
    items.sort(key=lambda x: x["object_key"], reverse=True)
    return items


def reset_form_state():
    st.session_state["form_progress_travaux"] = None
    st.session_state["form_progress_paiements"] = None
    st.session_state["form_date_pv"] = None
    st.session_state["form_commentaires"] = ""
    st.session_state["uploader_version"] += 1


# =========================
# 3) AUTH UI
# =========================

def render_auth_panel() -> None:
    """Affiche le panneau d’auth si demandé."""
    if not st.session_state.get("auth_panel_visible", True):
        return

    mode = st.radio(" ", ["Se connecter", "Créer un compte"], horizontal=False, label_visibility="hidden")
    email = st.text_input("Email", value="", key="auth_email")
    pwd = st.text_input("Mot de passe", value="", type="password", key="auth_pwd")

    colb1, colb2 = st.columns([1, 1])
    with colb1:
        if st.button("Connexion", use_container_width=True):
            try:
                res = sb.auth.sign_in_with_password({"email": email, "password": pwd})
                # on authentifie PostgREST explicitement
                sb.postgrest.auth(res.session.access_token)
                st.session_state["user"] = res.user
                st.session_state["auth_panel_visible"] = False
                st.toast("Connecté ✔️")
            except Exception as e:
                st.error(f"Échec de connexion : {e}")

    with colb2:
        if st.button("Se déconnecter", use_container_width=True):
            try:
                sb.auth.sign_out()
            finally:
                # on purge
                for k in ["user"]:
                    st.session_state[k] = None
                st.session_state["auth_panel_visible"] = True
                st.rerun()

    if mode == "Créer un compte":
        st.caption("Pour créer un compte, saisis ton email & mot de passe puis clique **Connexion**. Tu recevras un email de confirmation (pense à valider).")


# =========================
# 4) APP
# =========================

def main():
    # Deux mises en page : sans colonne gauche si connecté
    if st.session_state["user"] is None:
        col_auth, col_app = st.columns([0.9, 1.6], vertical_alignment="start")
    else:
        col_app = st.container()
        col_auth = None
        st.session_state["auth_panel_visible"] = False  # par sécurité

    # ---- Colonne Auth (si non connecté)
    if col_auth is not None:
        with col_auth:
            st.subheader("Connexion")
            render_auth_panel()

    # ---- Contenu principal
    with col_app:
        st.header("Suivi d’avancement — Saisie")

        # Ne rien montrer tant qu’on n’est pas connecté
        if st.session_state["user"] is None:
            st.info("Connecte-toi pour saisir une mise à jour.")
            return

        # Projets
        projs = fetch_projects()
        if not projs:
            st.warning("Aucun projet trouvé dans la table **projects**.")
            return
        options = {p["name"]: p["id"] for p in projs}
        # sélection
        name_default = next(iter(options.keys()))
        if (st.session_state["selected_project_id"] is None) or (st.session_state["selected_project_id"] not in options.values()):
            st.session_state["selected_project_id"] = options[name_default]

        chosen = st.selectbox(
            "Projet",
            options=list(options.keys()),
            index=list(options.values()).index(st.session_state["selected_project_id"]),
        )
        project_id = options[chosen]

        # reset si changement de projet
        if st.session_state["last_project_id"] != project_id:
            reset_form_state()
            st.session_state["last_project_id"] = project_id

        st.subheader("Nouvelle mise à jour")

        with st.form("update_form", clear_on_submit=False):
            c1, c2 = st.columns([1, 1])

            with c1:
                pt = st.number_input(
                    "Progression travaux (%)",
                    min_value=0.0, max_value=100.0, step=0.25,
                    value=st.session_state["form_progress_travaux"],
                    key="form_progress_travaux",
                    format="%.2f",
                )
            with c2:
                pp = st.number_input(
                    "Progression paiements (%)",
                    min_value=0.0, max_value=100.0, step=0.25,
                    value=st.session_state["form_progress_paiements"],
                    key="form_progress_paiements",
                    format="%.2f",
                )

            c3, = st.columns([1])
            with c3:
                d = st.date_input(
                    "Date du PV de chantier (optionnel)",
                    value=st.session_state["form_date_pv"] or date.today(),
                    key="form_date_pv",
                )

            com = st.text_area(
                "Commentaires",
                value=st.session_state["form_commentaires"],
                key="form_commentaires",
                placeholder="Observations, risques, points bloquants…",
                height=160,
            )

            st.markdown("**Joindre le PV (PDF/DOCX/DOC)**")
            uploaded = st.file_uploader(
                " ",
                type=[e.strip(".") for e in ALLOWED_EXT],
                accept_multiple_files=True,
                key=f"pv_uploader_v{st.session_state['uploader_version']}",
                label_visibility="hidden",
                help=f"Limite {MAX_UPLOAD_MB} Mo par fichier",
            )

            submitted = st.form_submit_button("Enregistrer la mise à jour", use_container_width=True)

        if submitted:
            try:
                user = st.session_state["user"]
                user_id = user.id if hasattr(user, "id") else user.get("id")

                insert_project_update(
                    project_id=project_id,
                    user_id=user_id,
                    progress_travaux=st.session_state["form_progress_travaux"],
                    progress_paiements=st.session_state["form_progress_paiements"],
                    pv_date=st.session_state["form_date_pv"],
                    commentaires=st.session_state["form_commentaires"],
                )

                ok, errs = upload_pv_files(project_id, user_id, uploaded or [])
                if errs:
                    st.warning("⚠️ Quelques fichiers n’ont pas été pris en compte :\n\n- " + "\n- ".join(errs))
                st.success(f"✅ Mise à jour enregistrée. Fichiers déposés : {ok}")

                # reset du formulaire
                reset_form_state()
                st.rerun()

            except Exception as e:
                st.error(f"Erreur enregistrement mise à jour : {e}")

        st.subheader("Pièces jointes — PV de chantier")
        pv_items = list_signed_pv(project_id, expires_sec=SIGNED_URL_EXPIRES)
        if not pv_items:
            st.info("Aucun PV (nouveau flux) pour ce projet.")
        else:
            for it in pv_items:
                fn = it["file_name"]
                url = it["url"]
                when = it.get("uploaded_at") or ""
                st.markdown(f"- **[{fn}]({url})**  \n  _Uploadé le : {when}_", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
